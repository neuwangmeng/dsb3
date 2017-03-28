"""
This script ensembles predictions of all the models. No bagging atm. just plain simple averaging to get started.
Final predictions are weighted average of all predictions. The weights are optimized on validation data.
"""
import numpy as np
import scipy
import theano
import theano.tensor as T
import collections
from sklearn.model_selection import StratifiedKFold

import evaluate_submission
import pathfinder
import utils
import utils_lung
import os.path as path
import os

CONFIGS = ['dsb_a02_c3_s1e_p8a1', 'dsb_a03_c3_s1e_p8a1']
expid = utils.generate_expid('ensemble')


def ensemble():
    valid_set_predictions, valid_set_labels = load_validation_set()

    weights = optimize_weights(valid_set_predictions, valid_set_labels)  # (config_name -> (weight) )

    y_valid_pred = weighted_average(valid_set_predictions, weights)

    test_set_predictions = {config: get_predictions_of_config(config, 'test') for config in CONFIGS}
    y_test_pred = weighted_average(test_set_predictions, weights)
    persist_predictions(y_test_pred, y_valid_pred)
    compare_test_performance_ind_vs_ensemble(test_set_predictions)


def load_validation_set():
    valid_set_predictions = {}  # (config_name -> (pid -> prediction) )
    for config in CONFIGS:
        valid_set_predictions[config] = get_predictions_of_config(config, 'valid')
    valid_set_labels = load_validation_labels()  # (pid -> prediction)
    sanity_check(valid_set_predictions, valid_set_labels)
    return valid_set_predictions, valid_set_labels


def get_predictions_of_config(config_name, which_set):
    metadata_dir = utils.get_dir_path('models', pathfinder.METADATA_PATH)
    metadata_path = utils.find_model_metadata(metadata_dir, config_name)
    metadata = utils.load_pkl(metadata_path)
    expid = metadata['experiment_id']
    predictions_dir = utils.get_dir_path('model-predictions', pathfinder.METADATA_PATH)
    outputs_path = predictions_dir + '/' + expid

    output_pkl_file = outputs_path + '/%s-%s.pkl' % (expid, which_set)
    preds = utils.load_pkl(output_pkl_file)  # pid2prediction
    preds = collections.OrderedDict(sorted(preds.items()))
    return preds


def load_validation_labels():
    train_valid_ids = utils.load_pkl(pathfinder.VALIDATION_SPLIT_PATH)
    valid_pids = train_valid_ids['validation']
    id2label = utils_lung.read_labels(pathfinder.LABELS_PATH)

    labels = {pid: id2label[pid] for pid in sorted(valid_pids)}
    return collections.OrderedDict(sorted(labels.items()))


def sanity_check(valid_set_predictions, valid_set_labels):
    for config in CONFIGS:
        # Check whether all configs exist
        # utils.find_model_metadata(pathfinder.METADATA_PATH, config)

        # Check whether all these configs contain all the predictions
        if valid_set_predictions[config].viewkeys() != valid_set_labels.viewkeys():
            raise ValueError(
                'the validation set predictions does not contain the same pids as the validation set labels')

    pass


def get_destination_path(filename):
    ensemble_predictions_dir = utils.get_dir_path('ensemble-predictions', pathfinder.METADATA_PATH)
    utils.auto_make_dir(ensemble_predictions_dir)

    destination_folder = path.join(ensemble_predictions_dir, expid)
    utils.auto_make_dir(destination_folder)
    destination_path = path.join(destination_folder, filename)
    return destination_path


def optimize_weights(predictions, labels):
    """

    :type predictions: dict
    :type labels: dict
    :param predictions: (config_name -> (pid -> prediction) )
    :param labels: ( (pid -> prediction) )
    :return  optimized weights as dict: (config_name -> (weight) )
    """
    print 'Optimizing weights...'
    # weights = simple_average(predictions.keys())
    X = predictions_dict_to_3d_array(predictions, labels)
    y = np.array(labels.values())

    # k fold cv
    n_folds = 5
    skf = StratifiedKFold(n_splits=n_folds, random_state=0)
    cv_result = []
    for train_index, test_index in skf.split(np.zeros(y.shape[0]), y):
        X_train, X_test = X[:, train_index, :], X[:, test_index, :]
        y_train, y_test = y[train_index], y[test_index]

        weights, loss = optimal_linear_weights(X_train, np.array(one_hot(y_train)))

        y_test_pred = np.zeros(len(test_index))
        for i, weight in enumerate(weights):
            y_test_pred += X_test[i, :, 1] * weights[i]  # this can probably be replaced with a tensor dot product

        valid_loss = utils_lung.log_loss(y_test, y_test_pred)
        cv_result.append({
            'weights': weights,
            'training_loss': loss,
            'validation_loss': valid_loss,
            'training_idx': train_index
        })

    # TODO do something with CV results!
    print cv_result

    weights, loss = optimal_linear_weights(X, np.array(one_hot(y)))
    print 'Optimal weights'
    config2weights = {}
    for model_nr in range(len(predictions.keys())):
        config = predictions.keys()[model_nr]
        print 'Weight for config {} is {}'.format(config, weights[model_nr])
        config2weights[config] = weights[model_nr]

    print 'Ensemble will use following weights distribution:'
    print config2weights
    return config2weights


def optimal_linear_weights(predictions_stack, targets):
    """

    :param predictions_stack:  predictions as numpy array with shape [num_configs x num_patients x 2]
    :param targets: target labels as one hot encoded 2D array with shape [num_patients x 2]
    :return:
    """
    X = theano.shared(predictions_stack.astype(theano.config.floatX))  # [num_configs x num_patients x 2]
    t = theano.shared(targets)
    W = T.vector('W')
    s = T.nnet.softmax(W).reshape((W.shape[0], 1, 1))
    weighted_avg_predictions = T.sum(X * s, axis=0)  # T.tensordot(X, s, [[0], [0]])
    error = log_loss(weighted_avg_predictions, t)
    grad = T.grad(error, W)
    f = theano.function([W], error)
    g = theano.function([W], grad)
    n_models = predictions_stack.shape[0]
    w_init = np.zeros(n_models, dtype=theano.config.floatX)
    out, loss, _ = scipy.optimize.fmin_l_bfgs_b(f, w_init, fprime=g, pgtol=1e-09, epsilon=1e-08, maxfun=10000)
    weights = np.exp(out)
    weights /= weights.sum()
    return weights, loss


def predictions_dict_to_3d_array(predictions, labels):
    """
    :return: predictions as numpy array with shape [num_configs x num_patients x 2]
    """
    n_models = len(predictions.keys())
    n_patients = len(labels)
    predictions_stack = np.zeros((n_models, n_patients, 2))  # num_configs x num_patients x 2 categories
    for model_nr in range(n_models):
        config = predictions.keys()[model_nr]
        for patient_nr, patient_id in enumerate(labels.keys()):
            predictions_stack[model_nr, patient_nr, 0] = 1.0 - predictions[config][patient_id]
            predictions_stack[model_nr, patient_nr, 1] = predictions[config][patient_id]
    return predictions_stack


def simple_average(configs):
    amount_of_configs = len(configs)
    equal_weight = 1.0 / amount_of_configs

    weights = {}
    for config in configs:
        weights[config] = equal_weight

    return weights


def weighted_average(predictions, weights):
    """
    Arithmetic average

    :param predictions: (config_name -> (pid -> prediction) )
    :param weights: (config_name -> (weight) )
    :return  predictions as dict: (pid -> prediction)
    """
    weighted_predictions = {}
    for config_name, config_predictions in predictions.iteritems():
        for pid, patient_prediction in config_predictions.iteritems():
            weighted_prediction = patient_prediction * weights[config_name]
            if pid in predictions:
                weighted_predictions[pid] += weighted_prediction
            else:
                weighted_predictions[pid] = weighted_prediction

    return collections.OrderedDict(sorted(weighted_predictions.items()))


def persist_predictions(y_test_pred, y_valid_pred):
    utils.save_pkl(y_valid_pred, get_destination_path('validation_set_predictions.pkl'))
    print 'Pickled ensemble predictions on validation set ({})'.format(
        get_destination_path('validation_set_predictions.pkl'))
    utils.save_pkl(y_test_pred, get_destination_path('test_set_predictions.pkl'))
    print 'Pickled ensemble predictions on test set ({})'.format(get_destination_path('test_set_predictions.pkl'))
    utils_lung.write_submission(y_test_pred, get_destination_path('test_set_predictions.csv'))
    print 'Saved ensemble predictions into csv file ({})'.format(get_destination_path('test_set_predictions.csv'))


def compare_test_performance_ind_vs_ensemble(test_set_predictions):
    individual_performance = {config: calc_test_performance(config, pred_test) for config, pred_test in
                              test_set_predictions.iteritems()}
    for config, performance in individual_performance.iteritems():
        print 'Logloss of config {} is {} on test set'.format(config, performance)
    loss = evaluate_submission.leaderboard_performance(get_destination_path('test_set_predictions.csv'))
    print('Ensemble test set performance as it would be on the leaderboard: ')
    print(loss)


def calc_test_performance(config_name, predictions):
    # make a tmp submission file to know leaderboard performance
    tmp_submission_file = '/tmp/submission_test_predictions_{}.csv'.format(config_name)
    utils_lung.write_submission(predictions, tmp_submission_file)
    loss = evaluate_submission.leaderboard_performance(tmp_submission_file)
    os.remove(tmp_submission_file)
    return loss


def log_loss(y, t, eps=1e-15):
    """
    cross entropy loss, summed over classes, mean over batches
    """
    y = T.clip(y, eps, 1 - eps)
    loss = -T.sum(t * T.log(y)) / y.shape[0].astype(theano.config.floatX)
    return loss


def log_losses(y, t, eps=1e-15):
    """
    cross entropy loss per example, summed over classes
    """
    y = T.clip(y, eps, 1 - eps)
    losses = -T.sum(t * T.log(y), axis=1)
    return losses


def one_hot(vec, m=None):
    if m is None:
        m = int(np.max(vec)) + 1

    return np.eye(m)[vec]


if __name__ == '__main__':
    print 'Starting ensembling with configs', CONFIGS
    ensemble()
    print 'Job done'