import tensorflow as tf
from tensorflow.contrib.layers import xavier_initializer_conv2d, variance_scaling_initializer, xavier_initializer
import tensorflow.contrib.slim as slim
import numpy as np


def image_size(_shape, stride):
    return int(np.ceil(_shape[0] / stride[0])), int(np.ceil(_shape[1] / stride[1]))


def convolution(x, weight_shape, stride, initializer):
    """ 2d convolution layer
    - weight_shape: width, height, input channel, output channel
    """
    weight = tf.Variable(initializer(shape=weight_shape))
    bias = tf.Variable(tf.zeros([weight_shape[-1]]), dtype=tf.float32)
    return tf.add(tf.nn.conv2d(x, weight, strides=[1, stride[0], stride[1], 1], padding="SAME"), bias)


def deconvolution(x, weight_shape, output_shape, stride, initializer):
    """ 2d deconvolution layer
    - weight_shape: width, height, input channel, output channel
    """
    weight = tf.Variable(initializer(shape=weight_shape))
    bias = tf.Variable(tf.zeros([weight_shape[2]]), dtype=tf.float32)
    _layer = tf.nn.conv2d_transpose(x, weight, output_shape=output_shape, strides=[1, stride[0], stride[1], 1],
                                    padding="SAME", data_format="NHWC")
    return tf.add(_layer, bias)


def full_connected(x, weight_shape, initializer):
    """ fully connected layer
    - weight_shape: input size, output size
    """
    weight = tf.Variable(initializer(shape=weight_shape))
    bias = tf.Variable(tf.zeros([weight_shape[-1]]), dtype=tf.float32)
    return tf.add(tf.matmul(x, weight), bias)


def reconstruction_loss(original, reconstruction):
    """
    The reconstruction loss (the negative log probability of the input under the reconstructed Bernoulli distribution
    induced by the decoder in the data space). This can be interpreted as the number of "nats" required for
    reconstructing the input when the activation in latent is given.
    Adding 1e-10 to avoid evaluation of log(0.0)
    """
    _tmp = original * tf.log(1e-10 + reconstruction) + (1 - original) * tf.log(1e-10 + 1 - reconstruction)
    return -tf.reduce_sum(_tmp, 1)


def latent_loss(latent_mean, latent_log_sigma_sq):
    """
    The latent loss, which is defined as the Kullback Leibler divergence between the distribution in latent space
    induced by the encoder on the data and some prior. This acts as a kind of regularizer. This can be interpreted as
    the number of "nats" required for transmitting the the latent space distribution given the prior.
    """
    return -0.5 * tf.reduce_sum(1 + latent_log_sigma_sq - tf.square(latent_mean) - tf.exp(latent_log_sigma_sq), 1)


class ConditionalVAE(object):
    """ Conditional VAE
    Inputs data must be normalized to be in range of 0 to 1
    (since VAE uses Bernoulli distribution for reconstruction loss)
    """

    def __init__(self, label_size, network_architecture=None, activation=tf.nn.softplus,
                 learning_rate=0.001, batch_size=100, save_path=None, load_model=None):
        """
        :param dict network_architecture: dictionary with following elements
            n_hidden_encoder_1: 1st layer encoder neurons
            n_hidden_encoder_2: 2nd layer encoder neurons
            n_hidden_decoder_1: 1st layer decoder neurons
            n_hidden_decoder_2: 2nd layer decoder neurons
            n_input: shape of input
            n_z: dimensionality of latent space

        :param activation: activation function (tensor flow function)
        :param float learning_rate:
        :param int batch_size:
        """
        if network_architecture is None:
            self.network_architecture = dict(n_hidden_encoder_1=500, n_hidden_encoder_2=500, n_hidden_decoder_1=500,
                                             n_hidden_decoder_2=500, n_input=[28, 28, 1], n_z=20)
        else:
            self.network_architecture = network_architecture
        self.activation = activation
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.label_size = label_size

        # Initializer
        if "relu" in self.activation.__name__:
            self.initializer_c, self.initializer = variance_scaling_initializer(), variance_scaling_initializer()
        else:
            self.initializer_c, self.initializer = xavier_initializer_conv2d(), xavier_initializer()

        # Create network
        self._create_network()

        # Summary
        tf.summary.scalar("loss", self.loss)
        # Launch the session
        self.sess = tf.Session(config=tf.ConfigProto(log_device_placement=False))
        # Summary writer for tensor board
        self.summary = tf.summary.merge_all()
        if save_path:
            self.writer = tf.summary.FileWriter(save_path, self.sess.graph)
        # Load model
        if load_model:
            tf.reset_default_graph()
            self.saver.restore(self.sess, load_model)

    def _create_network(self):
        """ Create Network, Define Loss Function and Optimizer """
        # tf Graph input
        self.x = tf.placeholder(tf.float32, [None] + self.network_architecture["n_input"], name="input")
        self.y = tf.placeholder(tf.int32, [None], name="output")

        # Build conditional input
        _label = tf.one_hot(self.y, self.label_size)
        _label = tf.reshape(_label, [-1, 1, 1, self.label_size])
        _one = tf.ones([self.batch_size] + self.network_architecture["n_input"][0:-1] + [self.label_size])
        _label = _one * _label
        _layer = tf.concat([self.x, _label], axis=3)
        _ch = self.network_architecture["n_input"][2] + self.label_size

        # Encoder network to determine mean and (log) variance of Gaussian distribution in latent space
        with tf.variable_scope("encoder"):
            # convolution 1
            _layer = convolution(_layer, [5, 5, _ch, 16], [2, 2], self.initializer_c)
            _layer = self.activation(_layer)
            # convolution 2
            _layer = convolution(_layer, [5, 5, 16, 32], [2, 2], self.initializer_c)
            _layer = self.activation(_layer)
            # convolution 3
            _layer = convolution(_layer, [5, 5, 32, 64], [2, 2], self.initializer_c)
            _layer = self.activation(_layer)
            # full connect to get "mean" and "sigma"
            _layer = slim.flatten(_layer)
            _shape = _layer.shape.as_list()
            _layer = full_connected(_layer, [_shape[-1], self.network_architecture["n_z"]], self.initializer)
            self.z_mean = self.activation(_layer)
            self.z_log_sigma_sq = self.activation(_layer)

        # Draw one sample z from Gaussian distribution
        eps = tf.random_normal((self.batch_size, self.network_architecture["n_z"]), mean=0, stddev=1, dtype=tf.float32)
        # z = mu + sigma*epsilon
        self.z = tf.add(self.z_mean, tf.multiply(tf.sqrt(tf.exp(self.z_log_sigma_sq)), eps))

        _label = tf.one_hot(self.y, self.label_size)
        _layer = tf.concat([self.z, _label], axis=1)

        # Decoder to determine mean of Bernoulli distribution of reconstructed input
        with tf.variable_scope("decoder"):
            _w0, _h0 = self.network_architecture["n_input"][0:-1]
            _w1, _h1 = image_size([_w0, _h0], [2, 2])
            _w2, _h2 = image_size([_w1, _h1], [2, 2])
            # _w3, _h3 = image_size([_w2, _h2], [2, 2])
            # full connect
            _in_size = self.network_architecture["n_z"] + self.label_size
            _layer = full_connected(_layer, [_in_size, int(_w2 * _h2 * 16)], self.initializer)
            _layer = self.activation(_layer)
            # reshape to the image
            _layer = tf.reshape(_layer, [-1, _w2, _h2, 16])
            # deconvolution 1
            _layer = deconvolution(_layer, [5, 5, 8, 16], [self.batch_size, _w1, _h1, 8], [2, 2], self.initializer_c)
            _layer = self.activation(_layer)
            # deconvolution 2
            _layer = deconvolution(_layer, [5, 5, 1, 8], [self.batch_size, _w0, _h0, 1], [2, 2], self.initializer_c)
            _layer = self.activation(_layer)
            # activation
            self.x_decoder_mean = tf.nn.sigmoid(_layer)

        # Define loss function
        with tf.name_scope('loss'):
            loss_1 = reconstruction_loss(original=self.x, reconstruction=self.x_decoder_mean)
            loss_2 = latent_loss(self.z_mean, self.z_log_sigma_sq)
            self.loss = tf.reduce_mean(loss_1 + loss_2)  # average over batch

        # Define optimizer
        optimizer = tf.train.AdamOptimizer(self.learning_rate)
        self.train = slim.learning.create_train_op(self.loss, optimizer)
        # saver
        self.saver = tf.train.Saver()

    def reconstruct(self, inputs):
        """Reconstruct given data. """
        assert len(inputs) == self.batch_size
        return self.sess.run(self.x_decoder_mean, feed_dict={self.x: inputs})

    def encode(self, inputs):
        """ Embed given data to latent vector. """
        return self.sess.run(self.z_mean, feed_dict={self.x: inputs})

    def decode(self, z=None):
        """ Generate data by sampling from latent space.
        If z_mu is not None, data for this point in latent space is generated.
        Otherwise, z_mu is drawn from prior in latent space.
        """
        z = np.random.normal(size=self.network_architecture["n_z"]) if z is None else z
        return self.sess.run(self.x_decoder_mean, feed_dict={self.z: z})


if __name__ == '__main__':
    ConditionalVAE(10)