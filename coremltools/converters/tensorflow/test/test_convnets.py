import unittest
import os, shutil, tempfile
import tensorflow as tf
import numpy as np

from tensorflow.python.tools.freeze_graph import freeze_graph
from tensorflow.tools.graph_transforms import TransformGraph

import coremltools

from testutils import generate_data, tf_transpose
from test_base import TFNetworkTest

# IMPORTANT NOTE TO ADD NEW TESTS:
# For each test function you should set up your own graph and session.
# Otherwise TF will carry all ops and tensors from previously run tests.

def conv_cell(inp, conv_weights, bias=None, activation=None, pooling=None, has_batchnorm=False, conv_config=None, data_format='NHWC'):
    if conv_config is None:
        conv_config = {'strides': [1,1,1,1], 'padding': 'SAME'}
    x = tf.nn.conv2d(inp, conv_weights, conv_config['strides'], conv_config['padding'], data_format=data_format)
    return x


class TFConvNetTest(TFNetworkTest):
    @classmethod
    def setUpClass(self):
        """
        Set up the unit test by loading common utilities.
        """
        pass

    # Backend - set use_cpu_only to be True when working on Intel GPU macs
    def _test_tf_model(
            self,
            graph,
            input_tensor_shapes,
            output_node_names,
            data_mode='random',
            delta=1e-2,
            use_cpu_only=True,
            use_freeze=True,
            quantize_tf_model=False):

        super(TFConvNetTest, self)._test_tf_model(
            graph,
            input_tensor_shapes,
            output_node_names,
            data_mode=data_mode,
            input_refs=None,
            delta=delta,
            use_cpu_only=use_cpu_only,
            use_freeze=use_freeze,
            quantize_tf_model=quantize_tf_model)

    def test_toy(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            matrix1 = tf.placeholder(tf.float32, shape=[1, 2], name="input")
            matrix2 = tf.Variable(tf.truncated_normal([2, 1]))
            product = tf.matmul(matrix1, matrix2, name="product")

        self._test_tf_model(graph, {"input": [1, 2]}, ["product"])

    def test_linear(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            # placeholder constructor returns a tensor not an op
            x = tf.placeholder(tf.float32, shape=[None, 20], name="test_linear/input")
            # Make a redundant tensor. It should get trimmed
            gt = tf.placeholder(tf.float32, shape=[None, 10])

            W = tf.Variable(tf.ones([20, 10]))
            b = tf.Variable(tf.ones([10]))

            y = tf.matmul(x, W) + b
            output_name = [y.op.name]
        # not batched
        self._test_tf_model(
            graph, {"test_linear/input": [1, 20]}, output_name)
        # batched
        self._test_tf_model(
            graph, {"test_linear/input": [8, 20]}, output_name)

    def test_log(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            # placeholder constructor returns a tensor not an op
            x = tf.placeholder(tf.float32, shape=[None, 20], name="test_log/input")
            # Make a redundant tensor. It should get trimmed
            gt = tf.placeholder(tf.float32, shape=[None, 10])

            W = tf.Variable(tf.ones([20, 10]))
            b = tf.Variable(tf.ones([10]))

            y = tf.log(tf.matmul(x, W) + b)
            output_name = [y.op.name]

        self._test_tf_model(
            graph, {"test_log/input": [1, 20]}, output_name)

    def test_convnet(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            inp = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="input")
            W1 = tf.Variable(tf.truncated_normal([3, 3, 3, 4], stddev=0.3))
            x = conv_cell(inp, W1)
            W2 = tf.Variable(tf.truncated_normal([3, 3, 4, 2], stddev=0.3))
            x = conv_cell(x, W2)

        output_name = [x.op.name]
        # not batched
        self._test_tf_model(graph, {"input": [1, 8, 8, 3]}, output_name)
        # TODO: batched
        # self._test_tf_model(graph, {"input:0": [10, 8, 8, 3]}, output_name)

    @unittest.skip
    def test_simple_convnet(self):
        def weight_variable(shape):
            initial = tf.truncated_normal(shape, stddev=0.1)
            return tf.Variable(initial)

        def bias_variable(shape):
            initial = tf.constant(0.1, shape=shape)
            return tf.Variable(initial)

        def conv2d(x, W):
            return tf.nn.conv2d(x, W, strides=[1, 1, 1, 1], padding='SAME')

        def max_pool_2x2(x):
            return tf.nn.max_pool(x, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME')

        graph = tf.Graph()
        with graph.as_default() as g:
            W_conv1 = weight_variable([5, 5, 1, 32])
            b_conv1 = bias_variable([32])

            x_image = tf.placeholder(
                tf.float32, shape=[None, 28, 28, 1], name="test_simple_conv/input")
            h_conv1 = tf.nn.relu(conv2d(x_image, W_conv1) + b_conv1)
            h_pool1 = max_pool_2x2(h_conv1)

            W_conv2 = weight_variable([5, 5, 32, 64])
            b_conv2 = bias_variable([64])

            h_conv2 = tf.nn.relu(conv2d(h_pool1, W_conv2) + b_conv2)
            h_pool2 = max_pool_2x2(h_conv2)

        output_name = [h_pool2.op.name]
        self._test_tf_model(
            graph, {"test_simple_conv/input:0": [1, 28, 28, 1]}, output_name, delta=1e-2)

    @unittest.skip
    def test_convnet_classifier(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_convnet/input")
            W_conv1 = tf.Variable(tf.truncated_normal([3, 3, 3, 2], stddev=0.3))
            h_conv1 = tf.nn.conv2d(x_image, W_conv1, strides=[1, 1, 1, 1], padding='SAME')
            h_conv1_flat = tf.reshape(h_conv1, [-1, 8 * 8 * 2])
            W_fc1 = tf.Variable(tf.truncated_normal([8 * 8 * 2, 4], stddev=0.3))
            h_fc1 = tf.matmul(h_conv1_flat, W_fc1)

        output_name = [h_fc1.op.name]
        # not batched
        self._test_tf_model(graph, {"test_convnet/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)
        # batched
        self._test_tf_model(graph, {"test_convnet/input:0": [10, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_convnet_quantized(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_convnet/input")
            W_conv1 = tf.Variable(tf.truncated_normal([3, 3, 3, 2], stddev=0.3))
            h_conv1 = tf.nn.conv2d(x_image, W_conv1, strides=[1, 1, 1, 1], padding='SAME')
            h_conv1_flat = tf.reshape(h_conv1, [-1, 8 * 8 * 2])
            W_fc1 = tf.Variable(tf.truncated_normal([8 * 8 * 2, 4], stddev=0.3))
            h_fc1 = tf.matmul(h_conv1_flat, W_fc1)

        output_name = [h_fc1.op.name]
        # quantized
        self._test_tf_model(
            graph, {"test_convnet/input:0": [1, 8, 8, 3]},
            output_name,
            delta=0.20,
            quantize_tf_model=True)

    @unittest.skip
    def test_reduce_max(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            # placeholder constructor returns a tensor not an op
            x = tf.placeholder(tf.float32, shape=[None, 20], name="test_reduce_max/input")
            W = tf.Variable(tf.ones([20, 10]))
            y = tf.matmul(x, W)
            output = tf.reduce_max(y, axis=-1)
            output_name = [output.op.name]
        # not batched
        self._test_tf_model(graph, {"test_reduce_max/input:0": [1, 20]}, output_name, delta=1e-2)

    @unittest.skip
    def test_pad_conv_fuse(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x = tf.placeholder(tf.float32, shape=[None, 32, 18, 3], name="test_pad_conv/input")
            W = tf.Variable(tf.truncated_normal([9, 9, 3, 5], stddev=1))
            paddings = tf.constant([[0, 0], [5, 5], [1, 1], [0, 0]])
            x_pad = tf.pad(x, paddings, "CONSTANT")
            output = tf.nn.conv2d(x_pad, W, strides=[1, 1, 1, 1], padding='VALID')

        output_name = [output.op.name]
        self._test_tf_model(
            graph, {"test_pad_conv/input:0": [1, 32, 18, 3]}, output_name, delta=.05)

    @unittest.skip
    def test_dilated_conv(self):
        #params: (Hin,Win,K,pad,dilation)
        Cin = 3
        Cout = 5
        params = [(32, 18, 3, 3), (14, 13, 3, 4), (14, 19, 1, 3), (17, 18, 5, 3), (14, 20, 3, 3)]
        for param in params:
            Hin, Win, K, d = param
            graph = tf.Graph()
            with graph.as_default() as g:
                x = tf.placeholder(
                    tf.float32, shape=[None, Hin, Win, Cin], name="test_pad_conv/input")
                W = tf.Variable(tf.truncated_normal([K, K, Cin, Cout], stddev=1))
                output = tf.nn.convolution(
                    x, W, strides=[1, 1], padding='VALID', dilation_rate=[d, d])

            output_name = [output.op.name]
            self._test_tf_model(
                graph, {"test_pad_conv/input:0": [1, Hin, Win, Cin]}, output_name, delta=.05)


class TFSingleLayersTest(TFNetworkTest):
    """ Small models from tensorflow.layers
    """

    def test_dense(self):
        # dense layer with some activation
        graph = tf.Graph()
        with graph.as_default() as g:
            x = tf.placeholder(tf.float32, shape=[None, 10], name="test_dense/input")
            y = tf.layers.dense(
                inputs=x,
                units=16,
                activation=tf.sigmoid,
                bias_initializer=tf.random_uniform_initializer)

        output_name = [y.op.name]
        self._test_tf_model(
            graph, {"test_dense/input": [1, 10]},
            output_name,
            delta=1e-2,
            quantize_tf_model=False,
            use_cpu_only=True)

    @unittest.skip
    def test_dense_quantized(self):
        # dense layer with some activation
        graph = tf.Graph()
        with graph.as_default() as g:
            x = tf.placeholder(tf.float32, shape=[None, 10], name="test_dense/input")
            y = tf.layers.dense(
                inputs=x,
                units=16,
                activation=tf.sigmoid,
                bias_initializer=tf.random_uniform_initializer)

        output_name = [y.op.name]
        self._test_tf_model(
            graph, {"test_dense/input": [1, 10]}, output_name, delta=0.05, quantize_tf_model=True)

    def test_dense_concat(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x = tf.placeholder(tf.float32, shape=[None, 10], name="test_dense/input")
            y = tf.layers.dense(
                inputs=x,
                units=16,
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            z1 = tf.layers.dense(
                inputs=y,
                units=20,
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            z2 = tf.layers.dense(
                inputs=y,
                units=20,
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            z3 = tf.layers.dense(
                inputs=y,
                units=20,
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            z = tf.concat([z1, z2, z3], axis=1)

        output_name = [z.op.name]
        self._test_tf_model(
            graph, {"test_dense/input": [1, 10]}, output_name, delta=1e-2, use_cpu_only=True)

    def test_conv2d_no_bias(self):
        # conv layer with "fused activation"
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_conv2d/input")
            W = tf.Variable(tf.random_normal((5, 5, 3, 4)))  # [Kh,Kw,Cin,Cout]
            conv1 = tf.nn.conv2d(input=x_image, filter=W, strides=[1, 1, 1, 1], padding='SAME')

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d/input": [1, 8, 8, 3]},
            output_name,
            delta=1e-2,
            quantize_tf_model=False,
            use_cpu_only=True)

    def test_conv2d(self):
        # conv layer with "fused activation"
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_conv2d/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[5, 5],
                padding='same',
                activation=tf.nn.relu,
                # bias_initializer=tf.random_uniform_initializer
                bias_initializer=tf.constant_initializer([1, 2, 3, 4]))

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d/input": [1, 8, 8, 3]},
            output_name,
            delta=1e-2,
            quantize_tf_model=False,
            use_cpu_only=True)

    @unittest.skip
    def test_conv2d_quantized(self):
        # conv layer with "fused activation"
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_conv2d/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[5, 5],
                padding='same',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d/input:0": [1, 8, 8, 3]},
            output_name,
            delta=0.05,
            quantize_tf_model=True)

    @unittest.skip
    def test_conv2d_valid(self):
        # conv layer with "fused activation"
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 8, 3], name="test_conv2d_valid/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='valid',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_valid/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2d_stride2(self):
        # conv layer with "fused activation"
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 8, 3], name="test_conv2d_stride2/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='valid',
                strides=(2, 2),
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_stride2/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2d_dilated(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 32, 32, 3], name="test_conv2d_dilated/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='valid',
                dilation_rate=(3, 4),
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_dilated/input:0": [1, 32, 32, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2dt(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_conv2dt/input")
            conv1 = tf.layers.conv2d_transpose(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='same',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(graph, {"test_conv2dt/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2dt_valid(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 8, 3], name="test_conv2dt_valid/input")
            conv1 = tf.layers.conv2d_transpose(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='valid',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2dt_valid/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2dt_stride2(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 8, 3], name="test_conv2dt_stride2/input")
            conv1 = tf.layers.conv2d_transpose(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='valid',
                strides=(2, 2),
                bias_initializer=tf.random_uniform_initializer)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv2dt_stride2/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2d_avepool(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 16, 16, 3], name="test_conv2d_avepool/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='same',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            pool1 = tf.layers.average_pooling2d(inputs=conv1, pool_size=[2, 2], strides=2)

        output_name = [pool1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_avepool/input:0": [1, 16, 16, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2d_maxpool(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 16, 16, 3], name="test_conv2d_maxpool/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='same',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            pool1 = tf.layers.max_pooling2d(
                inputs=conv1, pool_size=[3, 3], strides=1, padding='same')

        output_name = [pool1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_maxpool/input:0": [1, 16, 16, 3]}, output_name, delta=1e-2)

    def test_conv2d_bn(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[1, 16, 16, 3], name="test_conv2d_bn/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=4,
                kernel_size=[3, 3],
                padding='same',
                activation=tf.nn.relu,
                bias_initializer=tf.random_uniform_initializer)
            bn1 = tf.layers.batch_normalization(inputs=conv1, axis=-1)

        output_name = [bn1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_bn/input": [1, 16, 16, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2d_spatial_bn(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 16, 16, 3], name="test_conv2d_bn/input")
            bn1 = tf.layers.batch_normalization(inputs=x_image, axis=2)

        output_name = [bn1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_bn/input:0": [1, 16, 16, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_separable_conv2d(self):
        # conv layer with "fused activation"
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 8, 3], name="test_separable_conv2d/input")
            conv1 = tf.layers.separable_conv2d(
                inputs=x_image, filters=4, kernel_size=[3, 3], padding='valid', depth_multiplier=2)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_separable_conv2d/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv1d(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 3], name="test_conv1d/input")
            conv1 = tf.layers.conv1d(
                inputs=x_image, filters=2, kernel_size=3, padding='valid', use_bias=True)

        output_name = [conv1.op.name]
        self._test_tf_model(
            graph, {"test_conv1d/input:0": [1, 8, 3]}, output_name, data_mode='linear', delta=.05)

    @unittest.skip
    def test_conv1d_dense(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 3], name="test_conv1d_dense/input")
            conv1 = tf.layers.conv1d(
                inputs=x_image,
                filters=2,
                kernel_size=3,
                padding='same',
                bias_initializer=tf.random_uniform_initializer)
            conv1_flat = tf.reshape(conv1, [-1, 8 * 2])
            y = tf.layers.dense(inputs=conv1_flat, units=6, activation=tf.nn.relu)

        output_name = [y.op.name]
        # not batched
        self._test_tf_model(
            graph, {"test_conv1d_dense/input:0": [1, 8, 3]}, output_name, delta=1e-2)
        # batched
        self._test_tf_model(
            graph, {"test_conv1d_dense/input:0": [10, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv1d_avepool(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 3], name="test_conv1d_avepool/input")
            conv1 = tf.layers.conv1d(inputs=x_image, filters=2, kernel_size=5, padding='same')
            pool1 = tf.layers.average_pooling1d(inputs=conv1, pool_size=2, strides=2)

        output_name = [pool1.op.name]
        self._test_tf_model(
            graph, {"test_conv1d_avepool/input:0": [1, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv1d_maxpool(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 8, 3], name="test_conv1d_maxpool/input")
            conv1 = tf.layers.conv1d(inputs=x_image, filters=2, kernel_size=3, padding='same')
            pool1 = tf.layers.max_pooling1d(inputs=conv1, pool_size=2, strides=1)

        output_name = [pool1.op.name]
        self._test_tf_model(
            graph, {"test_conv1d_maxpool/input:0": [1, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_conv2d_resize_bilinear(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(
                tf.float32, shape=[None, 16, 16, 3], name="test_conv2d_resize_bl/input")
            conv1 = tf.layers.conv2d(
                inputs=x_image,
                filters=3,
                kernel_size=[3, 3],
                padding='same',
                activation=tf.nn.relu)
            bl1 = tf.image.resize_bilinear(images=conv1, size=[32, 32])

        output_name = [bl1.op.name]
        self._test_tf_model(
            graph, {"test_conv2d_resize_bl/input:0": [1, 16, 16, 3]}, output_name, delta=1e-2)

    def test_concat_constants(self):
        graph = tf.Graph()
        x, y = np.meshgrid(np.linspace(0., 1., 256), np.linspace(0., 1., 256))
        x = np.reshape(x, [1, 256, 256, 1])
        y = np.reshape(y, [1, 256, 256, 1])
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 256, 256, 3], name="input_image")
            xx = tf.constant(x, dtype=tf.float32)
            yy = tf.constant(y, dtype=tf.float32)
            img_concatenated = tf.concat([x_image, xx, yy], -1, name='concat')

        output_name = [img_concatenated.op.name]
        self._test_tf_model_constant(
            graph, {"input_image": [1, 256, 256, 3]}, output_name, delta=1e-2)

    def test_split(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_input = tf.placeholder(tf.float32, shape=[None, 10, 10, 6], name="input")
            y1, y2 = tf.split(x_input, 2, axis=3)
            z = tf.add(y1, y2, name='output')

        output_name = [z.op.name]
        self._test_tf_model_constant(graph, {"input": [1, 10, 10, 6]}, output_name, delta=1e-2)

    def test_sqrt(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_input = tf.placeholder(tf.float32, shape=[None, 10, 10, 6], name='input')
            z = tf.sqrt(x_input, name='output')

        output_name = [z.op.name]
        self._test_tf_model_constant(graph, {'input': [1, 10, 10, 6]}, output_name, delta=1e-2)

    def test_pow(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_input = tf.placeholder(tf.float32, shape=[None, 5, 5, 6], name="input")
            z = tf.pow(x_input, 4, name='output')

        output_name = [z.op.name]
        self._test_tf_model_constant(graph, {"input": [1, 5, 5, 6]}, output_name, delta=1e-2)

    def test_leaky_relu(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_input = tf.placeholder(tf.float32, shape=[None, 5, 5, 6], name="input")
            z = tf.nn.leaky_relu(x_input, 0.2, name='output')

        output_name = [z.op.name]
        self._test_tf_model_constant(
            graph, {"input": [1, 5, 5, 6]}, output_name, delta=1e-2, data_mode="random_zero_mean")

    # @unittest.skip
    # def test_resize_bilinear_non_fractional(self):
    #   graph = tf.Graph()
    #   with graph.as_default() as g:
    #     x_input = tf.placeholder(tf.float32, shape=[None, 10, 10, 3], name="input")
    #     z = tf.image.resize_bilinear(x_input, size=[20, 30], align_corners=True)
    #   output_name = [z.op.name]
    #   self._test_tf_model_constant(graph, {"input:0":[1,10,10,3]}, output_name, delta=1e-2)
    #
    # @unittest.skip
    # def test_resize_bilinear_non_fractional_upsample_mode(self):
    #   graph = tf.Graph()
    #   with graph.as_default() as g:
    #     x_input = tf.placeholder(tf.float32, shape=[None, 10, 10, 3], name="input")
    #     z = tf.image.resize_bilinear(x_input, size=[20, 30], align_corners=False)
    #   output_name = [z.op.name]
    #   self._test_tf_model_constant(graph, {"input:0":[1,10,10,3]}, output_name, delta=1e-2)
    #
    # @unittest.skip
    # def test_resize_bilinear_fractional(self):
    #   graph = tf.Graph()
    #   with graph.as_default() as g:
    #     x_input = tf.placeholder(tf.float32, shape=[None, 10, 10, 3], name="input")
    #     z = tf.image.resize_bilinear(x_input, size=[25, 45], align_corners=False)
    #   output_name = [z.op.name]
    #   self._test_tf_model_constant(graph, {"input:0":[1,10,10,3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_crop_resize(self):
        graph = tf.Graph()
        roi = np.zeros((2, 4), dtype=np.float32)
        graph = tf.Graph()
        with graph.as_default() as g:
            # placeholder constructor returns a tensor not an op
            x = tf.placeholder(tf.float32, shape=[None, 20], name="test_linear/input")
            # Make a redundant tensor. It should get trimmed
            gt = tf.placeholder(tf.float32, shape=[None, 10])

            W = tf.Variable(tf.ones([20, 10]))
            b = tf.Variable(tf.ones([10]))

            y = tf.matmul(x, W) + b
            output_name = [y.op.name]
        # not batched
        self._test_tf_model(graph, {"test_linear/input:0": [1, 20]}, output_name, delta=1e-2)
        # batched
        self._test_tf_model(graph, {"test_linear/input:0": [8, 20]}, output_name, delta=1e-2)

    @unittest.skip
    def test_log(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            # placeholder constructor returns a tensor not an op
            x = tf.placeholder(tf.float32, shape=[None, 20], name="test_log/input")
            # Make a redundant tensor. It should get trimmed
            gt = tf.placeholder(tf.float32, shape=[None, 10])

            W = tf.Variable(tf.ones([20, 10]))
            b = tf.Variable(tf.ones([10]))

            y = tf.log(tf.matmul(x, W) + b)
            output_name = [y.op.name]

        self._test_tf_model(
            graph, {"test_log/input:0": [1, 20]}, output_name, delta=1e-2, use_cpu_only=True)

    @unittest.skip
    def test_simple_convnet(self):
        def weight_variable(shape):
            initial = tf.truncated_normal(shape, stddev=0.1)
            return tf.Variable(initial)

        def bias_variable(shape):
            initial = tf.constant(0.1, shape=shape)
            return tf.Variable(initial)

        def conv2d(x, W):
            return tf.nn.conv2d(x, W, strides=[1, 1, 1, 1], padding='SAME')

        def max_pool_2x2(x):
            return tf.nn.max_pool(x, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME')

        graph = tf.Graph()
        with graph.as_default() as g:
            W_conv1 = weight_variable([5, 5, 1, 32])
            b_conv1 = bias_variable([32])

            x_image = tf.placeholder(
                tf.float32, shape=[None, 28, 28, 1], name="test_simple_conv/input")
            h_conv1 = tf.nn.relu(conv2d(x_image, W_conv1) + b_conv1)
            h_pool1 = max_pool_2x2(h_conv1)

            W_conv2 = weight_variable([5, 5, 32, 64])
            b_conv2 = bias_variable([64])

            h_conv2 = tf.nn.relu(conv2d(h_pool1, W_conv2) + b_conv2)
            h_pool2 = max_pool_2x2(h_conv2)

        output_name = [h_pool2.op.name]
        self._test_tf_model(
            graph, {"test_simple_conv/input:0": [1, 28, 28, 1]}, output_name, delta=1e-2)

    @unittest.skip
    def test_convnet(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_convnet/input")
            W_conv1 = tf.Variable(tf.truncated_normal([3, 3, 3, 2], stddev=0.3))
            h_conv1 = tf.nn.conv2d(x_image, W_conv1, strides=[1, 1, 1, 1], padding='SAME')
            h_conv1_flat = tf.reshape(h_conv1, [-1, 8 * 8 * 2])
            W_fc1 = tf.Variable(tf.truncated_normal([8 * 8 * 2, 4], stddev=0.3))
            h_fc1 = tf.matmul(h_conv1_flat, W_fc1)

        output_name = [h_fc1.op.name]
        # not batched
        self._test_tf_model(graph, {"test_convnet/input:0": [1, 8, 8, 3]}, output_name, delta=1e-2)
        # batched
        self._test_tf_model(graph, {"test_convnet/input:0": [10, 8, 8, 3]}, output_name, delta=1e-2)

    @unittest.skip
    def test_convnet_quantized(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x_image = tf.placeholder(tf.float32, shape=[None, 8, 8, 3], name="test_convnet/input")
            W_conv1 = tf.Variable(tf.truncated_normal([3, 3, 3, 2], stddev=0.3))
            h_conv1 = tf.nn.conv2d(x_image, W_conv1, strides=[1, 1, 1, 1], padding='SAME')
            h_conv1_flat = tf.reshape(h_conv1, [-1, 8 * 8 * 2])
            W_fc1 = tf.Variable(tf.truncated_normal([8 * 8 * 2, 4], stddev=0.3))
            h_fc1 = tf.matmul(h_conv1_flat, W_fc1)

        output_name = [h_fc1.op.name]
        # quantized
        self._test_tf_model(
            graph, {"test_convnet/input:0": [1, 8, 8, 3]},
            output_name,
            delta=0.20,
            quantize_tf_model=True)

    @unittest.skip
    def test_reduce_max(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            # placeholder constructor returns a tensor not an op
            x = tf.placeholder(tf.float32, shape=[None, 20], name="test_reduce_max/input")
            W = tf.Variable(tf.ones([20, 10]))
            y = tf.matmul(x, W)
            output = tf.reduce_max(y, axis=-1)
            output_name = [output.op.name]
        # not batched
        self._test_tf_model(graph, {"test_reduce_max/input:0": [1, 20]}, output_name, delta=1e-2)

    @unittest.skip
    def test_pad_conv_fuse(self):
        graph = tf.Graph()
        with graph.as_default() as g:
            x = tf.placeholder(tf.float32, shape=[None, 32, 18, 3], name="test_pad_conv/input")
            W = tf.Variable(tf.truncated_normal([9, 9, 3, 5], stddev=1))
            paddings = tf.constant([[0, 0], [5, 5], [1, 1], [0, 0]])
            x_pad = tf.pad(x, paddings, "CONSTANT")
            output = tf.nn.conv2d(x_pad, W, strides=[1, 1, 1, 1], padding='VALID')

        output_name = [output.op.name]
        self._test_tf_model(
            graph, {"test_pad_conv/input:0": [1, 32, 18, 3]}, output_name, delta=.05)

    @unittest.skip
    def test_dilated_conv(self):
        #params: (Hin,Win,K,pad,dilation)
        Cin = 3
        Cout = 5
        params = [(32, 18, 3, 3), (14, 13, 3, 4), (14, 19, 1, 3), (17, 18, 5, 3), (14, 20, 3, 3)]
        for param in params:
            Hin, Win, K, d = param
            graph = tf.Graph()
            with graph.as_default() as g:
                x = tf.placeholder(
                    tf.float32, shape=[None, Hin, Win, Cin], name="test_pad_conv/input")
                W = tf.Variable(tf.truncated_normal([K, K, Cin, Cout], stddev=1))
                output = tf.nn.convolution(
                    x, W, strides=[1, 1], padding='VALID', dilation_rate=[d, d])

            output_name = [output.op.name]
            self._test_tf_model(
                graph, {"test_pad_conv/input:0": [1, Hin, Win, Cin]}, output_name, delta=.05)


if __name__ == '__main__':
    # unittest.main()
    suite = unittest.TestSuite()
    suite.addTest(TFConvNetTest("test_convnet"))
    # suite.addTest(TFSingleLayersTest("test_conv2d"))
    # suite.addTest(TFSingleLayersTest("test_dense"))
    # suite.addTest(TFSingleLayersTest("test_dense_concat"))
    # suite.addTest(TFSingleLayersTest("test_conv2d_bn"))
    unittest.TextTestRunner().run(suite)
