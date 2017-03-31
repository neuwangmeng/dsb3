import sys
sys.path.append(".")
import lasagne as nn
import numpy as np
import theano
import pathfinder
import utils
from configuration import config, set_configuration
import theano.tensor as T
# import blobs_detection
import logger
import time
import cPickle
import multiprocessing as mp
import buffering


jobs = []
theano.config.warn_float64 = 'raise'

if len(sys.argv) < 3:
    sys.exit("Usage: test_luna_scan.py <configuration_name> <segm_map_folder>")

config_name = sys.argv[1]
dst_path = sys.argv[2]
set_configuration('configs_seg_scan', config_name)

def extract_candidates(predictions_scan, pid):
    print 'saving segm map'
    start_time = time.time()
    segm = predictions_scan[0, 0]
    print segm.min(), segm.max(), segm.shape, segm.dtype
    segm = (segm*255.99).astype("uint8")
    with open(dst_path + '/%s.pkl' % pid, "wb") as f:
        cPickle.dump(segm, f, protocol=cPickle.HIGHEST_PROTOCOL)
    print 'saving time:', (time.time() - start_time) / 60.

# predictions path
predictions_dir = utils.get_dir_path('model-predictions', pathfinder.METADATA_PATH)
# outputs_path = predictions_dir + '/%s' % config_name
# utils.auto_make_dir(outputs_path)

# logs
logs_dir = utils.get_dir_path('logs', pathfinder.METADATA_PATH)
sys.stdout = logger.Logger(logs_dir + '/%s.log' % config_name)
sys.stderr = sys.stdout

# builds model and sets its parameters
model = config().build_model()

x_shared = nn.utils.shared_empty(dim=len(model.l_in.shape))
idx_z = T.lscalar('idx_z')
idx_y = T.lscalar('idx_y')
idx_x = T.lscalar('idx_x')

window_size = config().window_size
stride = config().stride
n_windows = config().n_windows

givens = {}
givens[model.l_in.input_var] = x_shared

get_predictions_patch = theano.function([],
                                        nn.layers.get_output(model.l_out, deterministic=True),
                                        givens=givens,
                                        on_unused_input='ignore')

data_iterator = config().data_iterator

print
print 'Data'
print 'n samples: %d' % data_iterator.nsamples

start_time = time.time()
for n, (x, lung_mask, tf_matrix, pid) in enumerate(
        buffering.buffered_gen_threaded(data_iterator.generate(), buffer_size=2)):
    print '-------------------------------------'
    print n, pid

    predictions_scan = np.zeros((1, 1, n_windows * stride, n_windows * stride, n_windows * stride))

    for iz in xrange(n_windows):
        for iy in xrange(n_windows):
            for ix in xrange(n_windows):
                start_time_patch = time.time()
                x_shared.set_value(x[:, :, iz * stride:(iz * stride) + window_size,
                                   iy * stride:(iy * stride) + window_size,
                                   ix * stride:(ix * stride) + window_size])
                predictions_patch = get_predictions_patch()

                predictions_scan[0, 0,
                iz * stride:(iz + 1) * stride,
                iy * stride:(iy + 1) * stride,
                ix * stride:(ix + 1) * stride] = predictions_patch

    if predictions_scan.shape != x.shape:
        pad_width = (np.asarray(x.shape) - np.asarray(predictions_scan.shape)) / 2
        pad_width = [(p, p) for p in pad_width]
        predictions_scan = np.pad(predictions_scan, pad_width=pad_width, mode='constant')

    if lung_mask is not None:
        predictions_scan *= lung_mask

    print 'saved plot'
    print 'time since start:', (time.time() - start_time) / 60.

    jobs = [job for job in jobs if job.is_alive]
    if len(jobs) >= 3:
        jobs[0].join()
        del jobs[0]
    jobs.append(
        mp.Process(target=extract_candidates, args=(predictions_scan, pid)))
    jobs[-1].daemon = True
    jobs[-1].start()

for job in jobs: job.join()