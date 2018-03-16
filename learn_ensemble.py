import numpy as np

import argparse
import pickle
import os
import shutil

import keras
from keras import backend as K

import utils
from datasets import DATASETS, get_data_generator



def build_ensemble(architecture, num_classes, embeddings):
    
    cls_network = utils.build_network(num_classes, architecture, classification = True)
    cls_network = keras.models.Model(cls_network.inputs, [cls_network.layers[-3 if architecture.lower() == 'simple' else -2].output, cls_network.output], name = 'cnn0')
    embed_networks = [utils.build_network(emb.shape[1], architecture, classification = False, name = 'cnn{}'.format(i+1)) for i, emb in enumerate(embeddings)]
    
    input_ = keras.layers.Input(cls_network.input.shape.as_list()[1:])
    cls_feat, cls_prob = cls_network(input_)
    outputs = [cls_prob] + [cnn(input_) for cnn in embed_networks]
    
    concat = keras.layers.concatenate([cls_feat] + outputs[1:], name = 'concat')
    prob = keras.layers.Dense(num_classes, activation = 'softmax', name = 'prob')(keras.layers.BatchNormalization()(concat))
    
    return keras.models.Model(input_, [prob] + outputs)


def gen_inputs(gen, embeddings):
    
    for X, y in gen:
        yield (X, [keras.utils.to_categorical(y, embeddings[0].shape[0]), keras.utils.to_categorical(y, embeddings[0].shape[0])] + [embed[y] for embed in embeddings])



if __name__ == '__main__':

    # Parse arguments
    parser = argparse.ArgumentParser(description = 'Learns a classifier on top of a concatenation of flat and hierarchical features.', formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    arggroup = parser.add_argument_group('Data parameters')
    arggroup.add_argument('--dataset', type = str, required = True, choices = DATASETS, help = 'Training dataset.')
    arggroup.add_argument('--data_root', type = str, required = True, help = 'Root directory of the dataset.')
    arggroup.add_argument('--embedding', action = 'append', required = True, help = 'Path to a pickle dump of embeddings generated by compute_class_embeddings.py.')
    arggroup = parser.add_argument_group('Training parameters')
    arggroup.add_argument('--architecture', type = str, default = 'simple', choices = utils.ARCHITECTURES, help = 'Type of network architecture.')
    arggroup.add_argument('--lr_schedule', type = str, default = 'SGDR', choices = utils.LR_SCHEDULES, help = 'Type of learning rate schedule.')
    arggroup.add_argument('--clipgrad', type = float, default = 10.0, help = 'Gradient norm clipping.')
    arggroup.add_argument('--max_decay', type = float, default = 0.0, help = 'Learning Rate decay at the end of training.')
    arggroup.add_argument('--epochs', type = int, default = None, help = 'Number of training epochs.')
    arggroup.add_argument('--batch_size', type = int, default = 100, help = 'Batch size.')
    arggroup.add_argument('--val_batch_size', type = int, default = None, help = 'Validation batch size.')
    arggroup.add_argument('--cls_weight', type = float, default = 0.1, help = 'Weight of the overall classification loss.')
    arggroup.add_argument('--gpus', type = int, default = 1, help = 'Number of GPUs to be used.')
    arggroup = parser.add_argument_group('Output parameters')
    arggroup.add_argument('--model_dump', type = str, default = None, help = 'Filename where the learned model should be written to.')
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
    embeddings = []
    embed_labels = None
    for path in args.embedding:
        with open(path, 'rb') as pf:
            dump = pickle.load(pf)
            if embed_labels is None:
                embed_labels = dump['ind2label']
            embeddings.append(dump['embedding'])
            del dump

    # Load dataset
    data_generator = get_data_generator(args.dataset, args.data_root, classes = embed_labels)

    # Construct and train model
    if args.gpus <= 1:
        model = build_ensemble(args.architecture, data_generator.num_classes, embeddings)
        par_model = model
    else:
        with K.tf.device('/cpu:0'):
            model = build_ensemble(args.architecture, data_generator.num_classes, embeddings)
        par_model = keras.utils.multi_gpu_model(model, gpus = args.gpus)
    
    if not args.no_progress:
        model.summary()
    
    callbacks, num_epochs = utils.get_lr_schedule(args.lr_schedule, data_generator.num_train, args.batch_size, schedule_args = { arg_name : arg_val for arg_name, arg_val in vars(args).items() if arg_val is not None })

    if args.log_dir:
        if os.path.isdir(args.log_dir):
            shutil.rmtree(args.log_dir, ignore_errors = True)
        callbacks.append(keras.callbacks.TensorBoard(log_dir = args.log_dir, write_graph = False))

    if args.max_decay > 0:
        decay = (1.0/args.max_decay - 1) / ((data_generator.num_train // args.batch_size) * (args.epochs if args.epochs else num_epochs))
    else:
        decay = 0.0
    
    par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, decay=decay, momentum=0.9, clipnorm = args.clipgrad),
                      loss = dict([('prob', 'categorical_crossentropy'), ('cnn0', 'categorical_crossentropy')] + [('cnn{}'.format(i), utils.squared_distance) for i in range(1, len(embeddings) + 1)]),
                      loss_weights = dict([('prob', args.cls_weight)] + [('cnn{}'.format(i), 1.0) for i in range(len(embeddings) + 1)]),
                      metrics = dict([('prob', 'accuracy'), ('cnn0', 'accuracy')] + [('cnn{}'.format(i), utils.nn_accuracy(embeddings[i-1])) for i in range(1, len(embeddings) + 1)])
     )

    par_model.fit_generator(
              gen_inputs(data_generator.flow_train(args.batch_size), embeddings),
              data_generator.num_train // args.batch_size,
              validation_data = gen_inputs(data_generator.flow_test(args.val_batch_size), embeddings),
              validation_steps = data_generator.num_test // args.val_batch_size,
              epochs = args.epochs if args.epochs else num_epochs, callbacks = callbacks, verbose = not args.no_progress)

    # Evaluate final performance
    print(par_model.evaluate_generator(gen_inputs(data_generator.flow_test(args.val_batch_size), embeddings), data_generator.num_test // args.val_batch_size))

    # Save model
    if args.model_dump:
        model.save(args.model_dump)
    
    # Save test image embeddings
    if args.feature_dump:
        pred_model = keras.models.Model(model.inputs, model.get_layer('concat').output)
        pred_features = pred_model.predict_generator(data_generator.flow_test(args.val_batch_size, False), data_generator.num_test // args.val_batch_size)
        with open(args.feature_dump,'wb') as dump_file:
            pickle.dump({ 'feat' : dict(enumerate(pred_features)) }, dump_file)
