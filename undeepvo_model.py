from keras.optimizers import Adam
from keras.models import Model
from keras.layers import Conv2D, Conv2DTranspose, concatenate, Cropping2D, Dense, Flatten
from layers import depth_to_disparity, disparity_difference, expand_dims, spatial_transformation
from losses import photometric_consistency_loss


class UnDeepVOModel(object):
    def __init__(self, left_input_k_1, left_input_k, right_input_k, mode='train', lr=0.1, alpha_image_loss=0.85,
                 img_rows=128, img_cols=512):
        # NOTE: disparity calculation
        # depth = baseline * focal / disparity
        # depth = 0.54 * 721 / (1242 * disp)

        self.img_rows = img_rows

        self.img_cols = img_cols

        self.baseline = 0.54  # meters

        self.focal_length = 718.856 / 1241  # image width = 1241 (note: must scale using this number)

        self.left = left_input_k

        self.right = right_input_k

        self.left_next = left_input_k_1

        self.left_est = None

        self.right_est = None

        self.depthmap = None

        self.depthmap_left = None

        self.depthmap_right = None

        self.disparity_left = None

        self.disparity_right = None

        self.disparity_diff_left = None

        self.disparity_diff_right = None

        self.right_to_left_disparity = None

        self.left_to_right_disparity = None

        self.model = None

        self.depthmap = None

        self.mode = mode

        self.lr = lr

        self.alpha_image_loss = alpha_image_loss

        self.build_depth_architecture()

        self.build_pose_architecture()

        self.build_outputs()

        self.build_model()

        if self.mode == 'test':
            return

    @staticmethod
    def conv(input, channels, kernel_size, strides, activation='elu'):

        return Conv2D(channels, kernel_size=kernel_size, strides=strides, padding='same', activation=activation)(input)

    @staticmethod
    def deconv(input, channels, kernel_size, scale):

        return Conv2DTranspose(channels, kernel_size=kernel_size, strides=scale, padding='same')(input)

    def conv_block(self, input, channels, kernel_size):
        conv1 = self.conv(input, channels, kernel_size, 1)

        conv2 = self.conv(conv1, channels, kernel_size, 2)

        return conv2

    def deconv_block(self, input, channels, kernel_size, skip):
        deconv1 = self.deconv(input, channels, kernel_size, 2)

        if skip is not None:
            concat1 = concatenate([deconv1, skip], 3)
        else:
            concat1 = deconv1

        iconv1 = self.conv(concat1, channels, kernel_size, 1)

        return iconv1

    def get_depth(self, input):
        return self.conv(input, 2, 3, 1, 'sigmoid')

    def build_pose_architecture(self):
        input = concatenate([self.left, self.left_next], axis=3)

        conv1 = self.conv(input, 16, 7, 1, activation='relu')

        conv2 = self.conv(conv1, 32, 5, 1, activation='relu')

        conv3 = self.conv(conv2, 64, 3, 1, activation='relu')

        conv4 = self.conv(conv3, 128, 3, 1, activation='relu')

        conv5 = self.conv(conv4, 256, 3, 1, activation='relu')

        conv6 = self.conv(conv5, 512, 3, 1, activation='relu')

        flat1 = Flatten()(conv6)

        # translation

        fc1_tran = Dense(512, input_shape=(8192,))(flat1)

        fc2_tran = Dense(512, input_shape=(512,))(fc1_tran)

        fc3_tran = Dense(3, input_shape=(512,))(fc2_tran)

        self.translation = fc3_tran

        # rotation

        fc1_rot = Dense(512, input_shape=(512,))(flat1)

        fc2_rot = Dense(512, input_shape=(512,))(fc1_rot)

        fc3_rot = Dense(3, input_shape=(512,))(fc2_rot)

        self.rotation = fc3_rot

    def build_depth_architecture(self):
        # encoder
        conv1 = self.conv_block(self.left, 32, 7)

        conv2 = self.conv_block(conv1, 64, 5)

        conv3 = self.conv_block(conv2, 128, 3)

        conv4 = self.conv_block(conv3, 256, 3)

        conv5 = self.conv_block(conv4, 512, 3)

        conv6 = self.conv_block(conv5, 512, 3)

        conv7 = self.conv_block(conv6, 512, 3)

        # skips
        skip1 = conv1

        skip2 = conv2

        skip3 = conv3

        skip4 = conv4

        skip5 = conv5

        skip6 = conv6

        deconv7 = self.deconv_block(conv7, 512, 3, skip6)

        deconv6 = self.deconv_block(deconv7, 512, 3, skip5)

        deconv5 = self.deconv_block(deconv6, 256, 3, skip4)

        deconv4 = self.deconv_block(deconv5, 128, 3, skip3)

        deconv3 = self.deconv_block(deconv4, 64, 3, skip2)

        deconv2 = self.deconv_block(deconv3, 32, 3, skip1)

        deconv1 = self.deconv_block(deconv2, 16, 3, None)

        self.depthmap = self.get_depth(deconv1)

    def build_outputs(self):

        # store depthmaps

        self.depthmap_left = expand_dims(self.depthmap, 0, 'depth_map_exp_left')

        self.depthmap_right = expand_dims(self.depthmap, 1, 'depth_map_exp_right')

        if self.mode == 'test':
            return

        # generate disparities

        self.disparity_left = depth_to_disparity(self.depthmap_left, self.baseline, self.focal_length, 1,
                                                 'disparity_left')

        self.disparity_right = depth_to_disparity(self.depthmap_right, self.baseline, self.focal_length, 1,
                                                  'disparity_right')

        # generate estimates of left and right images

        self.left_est = spatial_transformation([self.right, self.disparity_right], -1, 'left_est')

        self.right_est = spatial_transformation([self.left, self.disparity_left], 1, 'right_est')

        # generate left - right consistency

        self.right_to_left_disparity = spatial_transformation([self.disparity_right, self.disparity_right], -1,
                                                              'r2l_disparity')

        self.left_to_right_disparity = spatial_transformation([self.disparity_left, self.disparity_left], 1,
                                                              'l2r_disparity')

        self.disparity_diff_left = disparity_difference([self.disparity_left, self.right_to_left_disparity],
                                                        'disp_diff_left')

        self.disparity_diff_right = disparity_difference([self.disparity_right, self.left_to_right_disparity],
                                                         'disp_diff_right')

    def build_model(self):
        self.model = Model(inputs=[self.left_next, self.left, self.right], outputs=[self.left_est,
                                                                                    self.right_est,
                                                                                    self.disparity_diff_left,
                                                                                    self.disparity_diff_right,
                                                                                    self.translation,
                                                                                    self.rotation])
        self.model.compile(loss=[photometric_consistency_loss(self.alpha_image_loss),
                                 photometric_consistency_loss(self.alpha_image_loss),
                                 'mean_absolute_error',
                                 'mean_absolute_error',
                                 'mean_absolute_error',
                                 'mean_absolute_error'],
                           optimizer=Adam(lr=self.lr),
                           # metrics=['accuracy']
                           )
