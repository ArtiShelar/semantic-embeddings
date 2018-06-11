import numpy as np

import argparse
import pickle
import os
import shutil

import keras
from keras import backend as K

import utils
from datasets import DATASETS, get_data_generator



def cls_model(embed_model, num_classes, cls_base = None):
    
    if cls_base is None:
        base = embed_model.output
    else:
        try:
            base = embed_model.layers[int(cls_base)].output
        except ValueError:
            base = embed_model.get_layer(cls_base).output
    
    x = keras.layers.Activation('relu')(base)
    x = keras.layers.BatchNormalization()(x)
    x = keras.layers.Dense(num_classes, activation = 'softmax', kernel_regularizer = keras.regularizers.l2(5e-4), name = 'prob')(x)
    return keras.models.Model(embed_model.inputs, [embed_model.output, x])


def transform_inputs(X, y, embedding, num_classes = None):
    
    return (X, embedding[y]) if num_classes is None else (X, [embedding[y], keras.utils.to_categorical(y, num_classes)])



if __name__ == '__main__':

    # Parse arguments
    parser = argparse.ArgumentParser(description = 'Learns to map images onto class embeddings.', formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    arggroup = parser.add_argument_group('Data parameters')
    arggroup.add_argument('--dataset', type = str, required = True, choices = DATASETS, help = 'Training dataset.')
    arggroup.add_argument('--data_root', type = str, required = True, help = 'Root directory of the dataset.')
    arggroup.add_argument('--embedding', type = str, required = True, help = 'Path to a pickle dump of embeddings generated by compute_class_embeddings.py.')
    arggroup = parser.add_argument_group('Training parameters')
    arggroup.add_argument('--architecture', type = str, default = 'simple', choices = utils.ARCHITECTURES, help = 'Type of network architecture.')
    arggroup.add_argument('--loss', type = str, default = 'mse', choices = ['mse', 'inv_corr'],
                          help = 'Loss function for learning embeddings. Use "mse" (mean squared error) for distance-based and "inv_corr" (negated dot product) for similarity-based L2-normalized embeddings.')
    arggroup.add_argument('--cls_weight', type = float, default = 0.0, help = 'If set to a positive value, an additional classification layer will be added and this parameter specifies the weight of the softmax loss.')
    arggroup.add_argument('--cls_base', type = str, default = None, help = 'Name or index of the layer that the classification layer should be based on. If not specified, the final embedding layer will be used.')
    arggroup.add_argument('--lr_schedule', type = str, default = 'SGDR', choices = utils.LR_SCHEDULES, help = 'Type of learning rate schedule.')
    arggroup.add_argument('--clipgrad', type = float, default = 10.0, help = 'Gradient norm clipping.')
    arggroup.add_argument('--max_decay', type = float, default = 0.0, help = 'Learning Rate decay at the end of training.')
    arggroup.add_argument('--epochs', type = int, default = None, help = 'Number of training epochs.')
    arggroup.add_argument('--batch_size', type = int, default = 100, help = 'Batch size.')
    arggroup.add_argument('--val_batch_size', type = int, default = None, help = 'Validation batch size.')
    arggroup.add_argument('--snapshot', type = str, default = None, help = 'Path where snapshots should be stored after every epoch. If existing, it will be used to resume training.')
    arggroup.add_argument('--initial_epoch', type = int, default = 0, help = 'Initial epoch for resuming training from snapshot.')
    arggroup.add_argument('--gpus', type = int, default = 1, help = 'Number of GPUs to be used.')
    arggroup = parser.add_argument_group('Output parameters')
    arggroup.add_argument('--model_dump', type = str, default = None, help = 'Filename where the learned model definition and weights should be written to.')
    arggroup.add_argument('--weight_dump', type = str, default = None, help = 'Filename where the learned model weights should be written to (without model definition).')
    arggroup.add_argument('--feature_dump', type = str, default = None, help = 'Filename where learned embeddings for test images should be written to.')
    arggroup.add_argument('--log_dir', type = str, default = None, help = 'Tensorboard log directory.')
    arggroup.add_argument('--no_progress', action = 'store_true', default = False, help = 'Do not display training progress, but just the final performance.')
    arggroup = parser.add_argument_group('Parameters for --lr_schedule=SGD')
    arggroup.add_argument('--sgd_patience', type = int, default = None, help = 'Patience of learning rate reduction in epochs.')
    arggroup.add_argument('--sgd_lr', type = float, default = 0.1, help = 'Initial learning rate.')
    arggroup.add_argument('--sgd_min_lr', type = float, default = None, help = 'Minimum learning rate.')
    arggroup = parser.add_argument_group('Parameters for --lr_schedule=SGDR')
    arggroup.add_argument('--sgdr_base_len', type = int, default = None, help = 'Length of first cycle in epochs.')
    arggroup.add_argument('--sgdr_mul', type = int, default = None, help = 'Multiplier for cycle length after each cycle.')
    arggroup.add_argument('--sgdr_max_lr', type = float, default = None, help = 'Maximum learning rate.')
    arggroup = parser.add_argument_group('Parameters for --lr_schedule=CLR')
    arggroup.add_argument('--clr_step_len', type = int, default = None, help = 'Length of each step in epochs.')
    arggroup.add_argument('--clr_min_lr', type = float, default = None, help = 'Minimum learning rate.')
    arggroup.add_argument('--clr_max_lr', type = float, default = None, help = 'Maximum learning rate.')
    args = parser.parse_args()
    
    if args.val_batch_size is None:
        args.val_batch_size = args.batch_size

    # Configure environment
    K.set_session(K.tf.Session(config = K.tf.ConfigProto(gpu_options = { 'allow_growth' : True })))

    # Load class embeddings
    with open(args.embedding, 'rb') as pf:
        embedding = pickle.load(pf)
        embed_labels = embedding['ind2label']
        embedding = embedding['embedding']

    # Load dataset
    data_generator = get_data_generator(args.dataset, args.data_root, classes = embed_labels)

    # Construct and train model
    if args.gpus <= 1:
        if args.snapshot and os.path.exists(args.snapshot):
            print('Resuming from snapshot {}'.format(args.snapshot))
            model = keras.models.load_model(args.snapshot, custom_objects = utils.get_custom_objects(args.architecture), compile = False)
        else:
            model = utils.build_network(embedding.shape[1], args.architecture)
            if args.loss == 'inv_corr':
                model = keras.models.Model(model.inputs, keras.layers.Lambda(utils.l2norm, name = 'l2norm')(model.output))
            if args.cls_weight > 0:
                model = cls_model(model, data_generator.num_classes, args.cls_base)
        par_model = model
    else:
        with K.tf.device('/cpu:0'):
            if args.snapshot and os.path.exists(args.snapshot):
                print('Resuming from snapshot {}'.format(args.snapshot))
                model = keras.models.load_model(args.snapshot, custom_objects = utils.get_custom_objects(args.architecture), compile = False)
            else:
                model = utils.build_network(embedding.shape[1], args.architecture)
                if args.loss == 'inv_corr':
                    model = keras.models.Model(model.inputs, keras.layers.Lambda(utils.l2norm, name = 'l2norm')(model.output))
                if args.cls_weight > 0:
                    model = cls_model(model, data_generator.num_classes, args.cls_base)
        par_model = keras.utils.multi_gpu_model(model, gpus = args.gpus)
    embedding_layer_name = 'l2norm' if args.loss == 'inv_corr' else 'embedding'
    
    if not args.no_progress:
        model.summary()

    callbacks, num_epochs = utils.get_lr_schedule(args.lr_schedule, data_generator.num_train, args.batch_size, schedule_args = { arg_name : arg_val for arg_name, arg_val in vars(args).items() if arg_val is not None })

    if args.log_dir:
        if os.path.isdir(args.log_dir):
            shutil.rmtree(args.log_dir, ignore_errors = True)
        callbacks.append(keras.callbacks.TensorBoard(log_dir = args.log_dir, write_graph = False))
    
    if args.snapshot:
        callbacks.append(keras.callbacks.ModelCheckpoint(args.snapshot) if args.gpus <= 1 else utils.TemplateModelCheckpoint(model, args.snapshot))

    if args.max_decay > 0:
        decay = (1.0/args.max_decay - 1) / ((data_generator.num_train // args.batch_size) * (args.epochs if args.epochs else num_epochs))
    else:
        decay = 0.0
    loss = utils.inv_correlation if args.loss == 'inv_corr' else utils.squared_distance
    if args.cls_weight > 0:
        par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, decay=decay, momentum=0.9, clipnorm = args.clipgrad),
                          loss = { embedding_layer_name : loss, 'prob' : 'categorical_crossentropy' },
                          loss_weights = { embedding_layer_name : 1.0, 'prob' : args.cls_weight },
                          metrics = { embedding_layer_name : utils.nn_accuracy(embedding, dot_prod_sim = (args.loss == 'inv_corr')), 'prob' : 'accuracy' })
    else:
        par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, decay=decay, momentum=0.9, clipnorm = args.clipgrad),
                          loss = loss,
                          metrics = [utils.nn_accuracy(embedding, dot_prod_sim = (args.loss == 'inv_corr'))])

    batch_transform_kwargs = {
        'embedding' : embedding,
        'num_classes' : data_generator.num_classes if args.cls_weight > 0 else None
    }

    par_model.fit_generator(
              data_generator.train_sequence(args.batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
              validation_data = data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
              epochs = args.epochs if args.epochs else num_epochs, initial_epoch = args.initial_epoch,
              callbacks = callbacks, verbose = not args.no_progress,
              max_queue_size = 100, workers = 8, use_multiprocessing = True)

    # Evaluate final performance
    print(par_model.evaluate_generator(data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs)))

    # Save model
    if args.weight_dump:
        try:
            model.save_weights(args.weight_dump)
        except Exception as e:
            print('An error occurred while saving the model weights: {}'.format(e))
    if args.model_dump:
        try:
            model.save(args.model_dump)
        except Exception as e:
            print('An error occurred while saving the model: {}'.format(e))

    # Save test image embeddings
    if args.feature_dump:
        pred_features = par_model.predict_generator(data_generator.flow_test(1, False), data_generator.num_test)
        if args.cls_weight > 0:
            pred_features = pred_features[0]
        with open(args.feature_dump,'wb') as dump_file:
            pickle.dump({ 'feat' : dict(enumerate(pred_features)) }, dump_file)
