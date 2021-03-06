import os
import re
import time
from datetime import datetime
import numpy as np
import tensorflow as tf
from ops.data_loader_joints import inputs
from ops.tf_fun import regression_mse, correlation, make_dir, \
    fine_tune_prepare_layers, ft_optimizer_list
from ops.utils import get_dt
from models.vgg_fc_model import model_struct


def train_and_eval(config):
    """Train and evaluate the model."""
    print 'Model directory: %s' % config.model_output

    # Prepare model training
    dt_stamp = re.split(
        '\.', str(datetime.now()))[0].\
        replace(' ', '_').replace(':', '_').replace('-', '_')
    dt_dataset = config.model_type + '_' + dt_stamp + '/'
    config.train_checkpoint = os.path.join(
        config.model_output, dt_dataset)  # timestamp this run
    config.summary_dir = os.path.join(
        config.train_summaries, config.model_output, dt_dataset)
    dir_list = [config.train_checkpoint, config.summary_dir]
    [make_dir(d) for d in dir_list]

    # Prepare model inputs
    train_data = os.path.join(config.tfrecord_dir, config.train_tfrecords)
    validation_data = os.path.join(config.tfrecord_dir, config.val_tfrecords)

    # Prepare data on CPU
    with tf.device('/cpu:0'):
        train_images, train_labels = inputs(
            tfrecord_file=train_data,
            batch_size=config.train_batch,
            im_size=config.resize,
            target_size=config.image_target_size,
            model_input_shape=config.resize,
            train=config.data_augmentations,
            label_shape=config.num_classes,
            num_epochs=config.epochs)
        val_images, val_labels = inputs(
            tfrecord_file=validation_data,
            batch_size=1,
            im_size=config.resize,
            target_size=config.image_target_size,
            model_input_shape=config.resize,
            train=config.data_augmentations,
            label_shape=config.num_classes,
            num_epochs=config.epochs)
        tf.summary.image('train images', tf.cast(train_images, tf.float32))
        tf.summary.image('validation images', tf.cast(val_images, tf.float32))

    with tf.device('/gpu:0'):
        with tf.variable_scope('cnn') as scope:

            model = model_struct(
                vgg16_npy_path=config.vgg16_weight_path,
                fine_tune_layers=config.initialize_layers)
            train_mode = tf.get_variable(name='training', initializer=True)
            model.build(
                rgb=train_images,
                output_shape=len(config.labels),
                train_mode=train_mode,
                batchnorm=config.batch_norm,
                fe_keys=config.fe_keys
                )

            # Prepare the loss function
            loss = regression_mse(
                model.fc8, train_labels)

            # Add wd if necessary
            if config.wd_penalty is not None:
                _, l2_wd_layers = fine_tune_prepare_layers(
                    tf.trainable_variables(), config.wd_layers)
                l2_wd_layers = [
                    x for x in l2_wd_layers if 'biases' not in x.name]
                # import ipdb;ipdb.set_trace()
                loss += (
                    config.wd_penalty * tf.add_n(
                        [tf.nn.l2_loss(x) for x in l2_wd_layers]))

            other_opt_vars, ft_opt_vars = fine_tune_prepare_layers(
                tf.trainable_variables(), config.fine_tune_layers)

            if config.optimizer == 'adam':
                train_op, _ = ft_optimizer_list(
                    loss, [other_opt_vars, ft_opt_vars],
                    tf.train.AdamOptimizer,
                    [config.hold_lr, config.lr])
            else:
                raise 'Unidentified optimizer'
            train_score, _ = correlation(
                model.fc8, train_labels)  # training accuracy

            tf.summary.scalar("loss", loss)
            tf.summary.scalar("training correlation", train_score)

            # Setup validation op
            if validation_data is not False:
                scope.reuse_variables()
                # Validation graph is the same as training except no batchnorm
                val_model = model_struct()
                val_model.build(
                    rgb=val_images,
                    output_shape=config.num_classes),

                # Calculate validation accuracy
                val_score, _ = correlation(
                    val_model.fc8, val_labels)
                tf.summary.scalar("validation correlation", val_score)

    # Set up summaries and saver
    saver = tf.train.Saver(
        tf.global_variables(), max_to_keep=config.keep_checkpoints)
    summary_op = tf.summary.merge_all()

    # Initialize the graph
    sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))

    # Need to initialize both of these if supplying num_epochs to inputs
    sess.run(tf.group(tf.global_variables_initializer(),
             tf.local_variables_initializer()))
    summary_writer = tf.summary.FileWriter(config.summary_dir, sess.graph)

    # Set up exemplar threading
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coord)

    # Start training loop
    np.save(config.train_checkpoint, config)
    step, val_max, losses = 0, 0, []
    train_acc = 0
    try:
        while not coord.should_stop():
            start_time = time.time()
            _, loss_value, train_acc = sess.run([train_op, loss, train_score])
            losses.append(loss_value)
            duration = time.time() - start_time
            assert not np.isnan(loss_value), 'Model diverged with loss = NaN'

            if step % 100 == 0 and step % 10 == 0:
                if validation_data is not False:
                    val_acc, val_pred, val_ims = sess.run(
                        [val_score, val_model.fc8, val_images])

                    np.savez(
                        os.path.join(
                            config.model_output, '%s_val_coors' % step),
                        val_pred=val_pred, val_ims=val_ims)
                else:
                    val_acc = -1  # Store every checkpoint

                # Summaries
                summary_str = sess.run(summary_op)
                summary_writer.add_summary(summary_str, step)

                # Training status and validation accuracy
                format_str = (
                    '%s: step %d, loss = %.2f (%.1f examples/sec; '
                    '%.3f sec/batch) | Training r = %s | '
                    'Validation r = %s | logdir = %s')
                print (format_str % (
                    datetime.now(), step, loss_value,
                    config.train_batch / duration, float(duration),
                    train_acc, val_acc, config.summary_dir))

                # Save the model checkpoint if it's the best yet
                if val_acc > val_max:
                    saver.save(
                        sess, os.path.join(
                            config.train_checkpoint,
                            'model_' + str(step) + '.ckpt'), global_step=step)

                    # Store the new max validation accuracy
                    val_max = val_acc

            else:
                # Training status
                format_str = ('%s: step %d, loss = %.2f (%.1f examples/sec; '
                              '%.3f sec/batch) | Training F = %s')
                print (format_str % (datetime.now(), step, loss_value,
                                     config.train_batch / duration,
                                     float(duration), train_acc))
            # End iteration
            step += 1

    except tf.errors.OutOfRangeError:
        print('Done training for %d epochs, %d steps.' % (config.epochs, step))
    finally:
        coord.request_stop()

        dt_stamp = get_dt()  # date-time stamp
        np.save(
            os.path.join(
                config.tfrecord_dir, '%straining_loss' % dt_stamp), losses)
    coord.join(threads)
    sess.close()
