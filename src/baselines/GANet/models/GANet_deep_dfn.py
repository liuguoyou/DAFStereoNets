import torch
import torch.nn as nn
import torch.nn.init as init
from ..libs.GANet.modules.GANet import DisparityRegression, GetCostVolume
from ..libs.GANet.modules.GANet import MyNormalize
from ..libs.GANet.modules.GANet import SGA
from ..libs.GANet.modules.GANet import LGA, LGA2, LGA3

# from GANet
#from ..libs.sync_bn.modules.sync_bn import BatchNorm2d, BatchNorm3d
#NOTE: Updated by CCJ on 2020/07/17, 17:35;
# DFN works well for all most the cases, except GANet, due to the sync_bn BatchNorm 2d or 3d used above;
# So here I change to use the following Sync BN from https://github.com/vacancy/Synchronized-BatchNorm-PyTorch;
from src.modules.sync_batchnorm import SynchronizedBatchNorm3d as BatchNorm3d 
from src.modules.sync_batchnorm import SynchronizedBatchNorm2d as BatchNorm2d 
#from src.modules.sync_batchnorm import SynchronizedBatchNorm1d as BatchNorm1d 

import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np

#added by CCJ:
from src.modules.cost_volume import cost_volume_faster
from src.modules.dfn import filterGenerator, DynamicFilterLayerOneChannel, DynamicFilterLayer

class BasicConv(nn.Module):

    def __init__(self, 
            in_channels, 
            out_channels, 
            deconv=False, 
            is_3d=False, 
            bn=True, 
            relu=True, 
            **kwargs):
        super(BasicConv, self).__init__()
#        print(in_channels, out_channels, deconv, is_3d, bn, relu, kwargs)
        self.relu = relu
        self.use_bn = bn
        if is_3d:
            if deconv:
                self.conv = nn.ConvTranspose3d(in_channels, out_channels, bias=False, **kwargs)
            else:
                self.conv = nn.Conv3d(in_channels, out_channels, bias=False, **kwargs)
            self.bn = BatchNorm3d(out_channels)
        else:
            if deconv:
                self.conv = nn.ConvTranspose2d(in_channels, out_channels, bias=False, **kwargs)
            else:
                self.conv = nn.Conv2d(in_channels, out_channels, bias=False, **kwargs)
            self.bn = BatchNorm2d(out_channels)
    
    def forward(self, x):
        x = self.conv(x)
        if self.use_bn:
            x = self.bn(x)
        if self.relu:
            x = F.relu(x, inplace=True)
        return x

""" 2x means doing convolution 2 times """
class Conv2x(nn.Module):

    def __init__(self, in_channels, out_channels, deconv=False, is_3d=False, concat=True, bn=True, relu=True):
        super(Conv2x, self).__init__()
        self.concat = concat
        
        if deconv and is_3d: 
            kernel = (3, 4, 4)
        elif deconv:
            kernel = 4
        else:
            kernel = 3
        self.conv1 = BasicConv(in_channels, out_channels, deconv, is_3d, bn=True, relu=True, kernel_size=kernel, stride=2, padding=1)

        if self.concat: 
            self.conv2 = BasicConv(out_channels*2, out_channels, False, is_3d, bn, relu, kernel_size=3, stride=1, padding=1)
        else:
            self.conv2 = BasicConv(out_channels, out_channels, False, is_3d, bn, relu, kernel_size=3, stride=1, padding=1)
    def forward(self, x, rem):
        x = self.conv1(x)
        assert x.size() == rem.size(), "x.size = {}, rem.size={}".format(x.size(), rem.size())
        if self.concat:
            x = torch.cat((x, rem), 1)
        else: 
            x = x + rem
        x = self.conv2(x)
        return x

class Feature(nn.Module):
    def __init__(self):
        super(Feature, self).__init__()

        self.conv_start = nn.Sequential(
            BasicConv(3, 32, kernel_size=3, padding=1),
            BasicConv(32, 32, kernel_size=5, stride=3, padding=2),
            BasicConv(32, 32, kernel_size=3, padding=1))
        self.conv1a = BasicConv(32, 48, kernel_size=3, stride=2, padding=1)
        self.conv2a = BasicConv(48, 64, kernel_size=3, stride=2, padding=1)
        self.conv3a = BasicConv(64, 96, kernel_size=3, stride=2, padding=1)
        self.conv4a = BasicConv(96, 128, kernel_size=3, stride=2, padding=1)

        self.deconv4a = Conv2x(128, 96, deconv=True)
        self.deconv3a = Conv2x(96, 64, deconv=True)
        self.deconv2a = Conv2x(64, 48, deconv=True)
        self.deconv1a = Conv2x(48, 32, deconv=True)

        self.conv1b = Conv2x(32, 48) # default: k=3,s=2,p=1
        self.conv2b = Conv2x(48, 64)
        self.conv3b = Conv2x(64, 96)
        self.conv4b = Conv2x(96, 128)

        self.deconv4b = Conv2x(128, 96, deconv=True)
        self.deconv3b = Conv2x(96, 64, deconv=True)
        self.deconv2b = Conv2x(64, 48, deconv=True)
        self.deconv1b = Conv2x(48, 32, deconv=True)

    def forward(self, x):
        x = self.conv_start(x)
        rem0 = x
        x = self.conv1a(x)
        rem1 = x
        x = self.conv2a(x)
        rem2 = x
        x = self.conv3a(x)
        rem3 = x
        x = self.conv4a(x)
        rem4 = x
        x = self.deconv4a(x, rem3)
        rem3 = x

        x = self.deconv3a(x, rem2)
        rem2 = x
        x = self.deconv2a(x, rem1)
        rem1 = x
        x = self.deconv1a(x, rem0)
        rem0 = x

        x = self.conv1b(x, rem1)
        rem1 = x
        x = self.conv2b(x, rem2)
        rem2 = x
        x = self.conv3b(x, rem3)
        rem3 = x
        x = self.conv4b(x, rem4)

        x = self.deconv4b(x, rem3)
        x = self.deconv3b(x, rem2)
        x = self.deconv2b(x, rem1)
        x = self.deconv1b(x, rem0)

        return x

class Guidance(nn.Module):
    def __init__(self):
        super(Guidance, self).__init__()

        self.conv0 = BasicConv(64, 16, kernel_size=3, padding=1)
        self.conv1 = nn.Sequential(
            BasicConv(16, 32, kernel_size=5, stride=3, padding=2),
            BasicConv(32, 32, kernel_size=3, padding=1))

        self.conv2 = BasicConv(32, 32, kernel_size=3, padding=1)
        self.conv3 = BasicConv(32, 32, kernel_size=3, padding=1)

#        self.conv11 = Conv2x(32, 48)
        self.conv11 = nn.Sequential(BasicConv(32, 48, kernel_size=3, stride=2, padding=1),
                                    BasicConv(48, 48, kernel_size=3, padding=1))
        self.conv12 = BasicConv(48, 48, kernel_size=3, padding=1)
        self.conv13 = BasicConv(48, 48, kernel_size=3, padding=1)
        self.conv14 = BasicConv(48, 48, kernel_size=3, padding=1)

        self.weight_sg1 = nn.Conv2d(32, 640, (3, 3), (1, 1), (1, 1), bias=False)
        self.weight_sg2 = nn.Conv2d(32, 640, (3, 3), (1, 1), (1, 1), bias=False)
        self.weight_sg3 = nn.Conv2d(32, 640, (3, 3), (1, 1), (1, 1), bias=False)

        self.weight_sg11 = nn.Conv2d(48, 960, (3, 3), (1, 1), (1, 1), bias=False)
        self.weight_sg12 = nn.Conv2d(48, 960, (3, 3), (1, 1), (1, 1), bias=False)
        self.weight_sg13 = nn.Conv2d(48, 960, (3, 3), (1, 1), (1, 1), bias=False)
        self.weight_sg14 = nn.Conv2d(48, 960, (3, 3), (1, 1), (1, 1), bias=False)

        self.weight_lg1 = nn.Sequential(BasicConv(16, 16, kernel_size=3, padding=1),
                                        nn.Conv2d(16, 75, (3, 3), (1, 1), (1, 1) ,bias=False))
        self.weight_lg2 = nn.Sequential(BasicConv(16, 16, kernel_size=3, padding=1),
                                        nn.Conv2d(16, 75, (3, 3), (1, 1), (1, 1) ,bias=False))

    def forward(self, x):
        x = self.conv0(x)
        rem = x
        x = self.conv1(x)
        sg1 = self.weight_sg1(x)
        x = self.conv2(x)
        sg2 = self.weight_sg2(x)
        x = self.conv3(x)
        sg3 = self.weight_sg3(x)

        x = self.conv11(x)
        sg11 = self.weight_sg11(x)
        x = self.conv12(x)
        sg12 = self.weight_sg12(x)
        x = self.conv13(x)
        sg13 = self.weight_sg13(x)
        x = self.conv14(x)
        sg14 = self.weight_sg14(x)

        lg1 = self.weight_lg1(rem)
        lg2 = self.weight_lg2(rem)
       
        return dict([
            ('sg1', sg1),
            ('sg2', sg2),
            ('sg3', sg3),
            ('sg11', sg11),
            ('sg12', sg12),
            ('sg13', sg13),
            ('sg14', sg14),
            ('lg1', lg1),
            ('lg2', lg2)])

class Disp(nn.Module):

    def __init__(self, maxdisp=192):
        super(Disp, self).__init__()
        self.maxdisp = maxdisp
        self.softmax = nn.Softmin(dim=1)
        self.disparity = DisparityRegression(maxdisp=self.maxdisp)
#        self.conv32x1 = BasicConv(32, 1, kernel_size=3)
        self.conv32x1 = nn.Conv3d(32, 1, kernel_size=(3, 3, 3), 
                                stride=(1, 1, 1), padding=(1, 1, 1), bias=False)

    def forward(self, x):
        x = F.interpolate(self.conv32x1(x), 
                        #NOTE: 
                        # comments added by CCJ on 2019/10/14:
                        # if input has shape [N, C,D,H,W], then the size for interpolation is
                        # in this order: size=[size_D, size_H, size_W]
                        # Note that the interpolation do not change the dim = N and C !!!
                        [self.maxdisp+1, x.size()[3]*3, x.size()[4]*3], 
                        mode='trilinear',
                        #"""
                        #align_corners (bool, optional) – Geometrically, we consider the pixels of the input and output 
                        #as squares rather than points. If set to True, the input and output tensors are aligned by the 
                        #center points of their corner pixels, preserving the values at the corner pixels. 
                        #If set to False, the input and output tensors are aligned by the corner points of their corner pixels, 
                        #and the interpolation uses edge value padding for out-of-boundary values, 
                        #making this operation independent of input size when scale_factor is kept the same. 
                        #This only has an effect when mode is 'linear', 'bilinear', 'bicubic' or 'trilinear'. 
                        #Default: False 
                        #""" 
                        align_corners=False
                        )
        x = torch.squeeze(x, 1)
        x = self.softmax(x)

        return self.disparity(x)

class DispAgg(nn.Module):

    def __init__(self, maxdisp=192):
        super(DispAgg, self).__init__()
        self.maxdisp = maxdisp
        self.LGA3 = LGA3(radius=2) # radius = 2, means kernel window size = 2*radius + 1 = 5;
        self.LGA2 = LGA2(radius=2)
        self.LGA = LGA(radius=2)
        self.softmax = nn.Softmin(dim=1)
        self.disparity = DisparityRegression(maxdisp=self.maxdisp)
#        self.conv32x1 = BasicConv(32, 1, kernel_size=3)
        self.conv32x1=nn.Conv3d(32, 1, (3, 3, 3), (1, 1, 1), (1, 1, 1), bias=False)

    def lga(self, x, g):
        g = F.normalize(g, p=1, dim=1)
        x = self.LGA2(x, g)
        return x

    def forward(self, x, lg1, lg2):
        x = F.interpolate(self.conv32x1(x), 
                    #NOTE: 
                    # comments added by CCJ on 2019/10/14:
                    # if input has shape [N, C,D,H,W], then the size for interpolation is
                    # in this order: size=[size_D, size_H, size_W]
                    # Note that the interpolation do not change the dim = N and C !!!
                    [self.maxdisp+1, x.size()[3]*3, x.size()[4]*3], 
                    mode='trilinear', align_corners=False)
        x = torch.squeeze(x, 1)
        assert(lg1.size() == lg2.size())
        x = self.lga(x, lg1)
        x = self.softmax(x)
        x = self.lga(x, lg2)
        x = F.normalize(x, p=1, dim=1)
        return self.disparity(x)

class SGABlock(nn.Module):
    def __init__(self, channels=32, refine=False):
        super(SGABlock, self).__init__()
        self.refine = refine
        if self.refine:
            self.bn_relu = nn.Sequential(BatchNorm3d(channels),
                                         nn.ReLU(inplace=True))
            self.conv_refine = BasicConv(channels, channels, is_3d=True, kernel_size=3, padding=1, relu=False)
#            self.conv_refine1 = BasicConv(8, 8, is_3d=True, kernel_size=1, padding=1)
        else:
            self.bn = BatchNorm3d(channels)
        self.SGA=SGA()
        self.relu = nn.ReLU(inplace=True)
    def forward(self, x, g):
        rem = x
        #NOTE:
        #Comments added by CCJ:
        # split g channel C (e.g., C= 640) to 4 parts, each with C/4 ( e.g., = 640/4=160) size along channel dim, i.e., dim=1;
        # each C/4=160-dim vector is further divided into 32 x 5, where 32 is the same as input x channel, 
        # and 5 means w0, w1, ..., w4 in Eq (5) in GANet CVPR paper, s.t. w0 + w1 + ... + w4 = 1.0, 
        # this why F.normalize() is applied along dim=5, that is normalize those five values, s.t. w0 + w1 + ... + w4 = 1.0 !!!
        k1, k2, k3, k4 = torch.split(g, (x.size()[1]*5, x.size()[1]*5, x.size()[1]*5, x.size()[1]*5), 1)
        # k1: SGA in down direction;
        k1 = F.normalize(k1.view(x.size()[0], x.size()[1], 5, x.size()[3], x.size()[4]), p=1, dim=2)#p=1 means L_p = L_1 norm;
        # k2: SGA in up direction;
        k2 = F.normalize(k2.view(x.size()[0], x.size()[1], 5, x.size()[3], x.size()[4]), p=1, dim=2)
        # k3: SGA in right direction;
        k3 = F.normalize(k3.view(x.size()[0], x.size()[1], 5, x.size()[3], x.size()[4]), p=1, dim=2)
        # k4: SGA in left direction;
        k4 = F.normalize(k4.view(x.size()[0], x.size()[1], 5, x.size()[3], x.size()[4]), p=1, dim=2)
        x = self.SGA(x, k1, k2, k3, k4)
        if self.refine:
            x = self.bn_relu(x)
            x = self.conv_refine(x)
        else:
            x = self.bn(x)
        assert(x.size() == rem.size())
        x += rem
        return self.relu(x)    
#        return self.bn_relu(x)


class CostAggregation(nn.Module):
    def __init__(self, maxdisp=192):
        super(CostAggregation, self).__init__()
        self.maxdisp = maxdisp
        self.conv_start = BasicConv(64, 32, is_3d=True, kernel_size=3, padding=1, relu=False)

        self.conv1a = BasicConv(32, 48, is_3d=True, kernel_size=3, stride=2, padding=1)
        self.conv2a = BasicConv(48, 64, is_3d=True, kernel_size=3, stride=2, padding=1)
#        self.conv3a = BasicConv(64, 96, is_3d=True, kernel_size=3, stride=2, padding=1)

        self.deconv1a = Conv2x(48, 32, deconv=True, is_3d=True, relu=False)
        self.deconv2a = Conv2x(64, 48, deconv=True, is_3d=True)
#        self.deconv3a = Conv2x(96, 64, deconv=True, is_3d=True)

        self.conv1b = Conv2x(32, 48, is_3d=True)
        self.conv2b = Conv2x(48, 64, is_3d=True)
#        self.conv3b = Conv2x(64, 96, is_3d=True)

        self.deconv1b = Conv2x(48, 32, deconv=True, is_3d=True, relu=False)
        self.deconv2b = Conv2x(64, 48, deconv=True, is_3d=True)
#        self.deconv3b = Conv2x(96, 64, deconv=True, is_3d=True)
        self.deconv0b = Conv2x(8, 8, deconv=True, is_3d=True)
        
        self.sga1 = SGABlock(refine=True)
        self.sga2 = SGABlock(refine=True)
        self.sga3 = SGABlock(refine=True)

        self.sga11 = SGABlock(channels=48, refine=True)
        self.sga12 = SGABlock(channels=48, refine=True)
        self.sga13 = SGABlock(channels=48, refine=True)
        self.sga14 = SGABlock(channels=48, refine=True)

        self.disp0 = Disp(self.maxdisp)
        self.disp1 = Disp(self.maxdisp)
        self.disp2 = DispAgg(self.maxdisp)


    def forward(self, x, g):
        """ 
          args:
             x: cost volume, in size [N,C,D/3,H/3,W/3], for convenience, call it [N,C,D,H,W];
             g: guidance dict, containing several tensors;
          return:
             
        """ 
        x = self.conv_start(x)
        x = self.sga1(x, g['sg1'])
        rem0 = x
       
        if self.training:
            disp0 = self.disp0(x)

        x = self.conv1a(x)
        x = self.sga11(x, g['sg11'])
        rem1 = x
        x = self.conv2a(x)
        rem2 = x
#        x = self.conv3a(x)
#        rem3 = x

#        x = self.deconv3a(x, rem2)
#        rem2 = x
        x = self.deconv2a(x, rem1)
        x = self.sga12(x, g['sg12'])
        rem1 = x
        x = self.deconv1a(x, rem0)
        x = self.sga2(x, g['sg2'])
        rem0 = x
        if self.training:
            disp1 = self.disp1(x)

        x = self.conv1b(x, rem1)
        x = self.sga13(x, g['sg13'])
        rem1 = x
        x = self.conv2b(x, rem2)
#        rem2 = x
#        x = self.conv3b(x, rem3)

#        x = self.deconv3b(x, rem2)
        x = self.deconv2b(x, rem1)
        x = self.sga14(x, g['sg14'])
        x = self.deconv1b(x, rem0)
        x = self.sga3(x, g['sg3'])

        disp2 = self.disp2(x, g['lg1'], g['lg2'])
        if self.training:
            return disp0, disp1, disp2
        else:
            return disp2

class GANet(nn.Module):
    def __init__(self, maxdisp=192,
            #added for DFN by CCJ;
            kernel_size = 5,
            dilation = 2,
            cost_filter_grad = True,
            isDFN = True, # True of False
            #isDFN = False, # True of False
        ):
        super(GANet, self).__init__()
        self.maxdisp = maxdisp
        self.conv_start = nn.Sequential(BasicConv(3, 16, kernel_size=3, padding=1),
                                        BasicConv(16, 32, kernel_size=3, padding=1))

        self.conv_x = BasicConv(32, 32, kernel_size=3, padding=1) # with default bn=True, relu=True
        self.conv_y = BasicConv(32, 32, kernel_size=3, padding=1) # with default bn=True, relu=True
        self.conv_refine = nn.Conv2d(32, 32, (3, 3), (1,1), (1,1), bias=False) #just convolution, no bn and relu;
        self.bn_relu = nn.Sequential(BatchNorm2d(32), nn.ReLU(inplace=True))
        self.feature = Feature()
        self.guidance = Guidance()
        self.cost_agg = CostAggregation(self.maxdisp)
        
        #self.cv = GetCostVolume(int(self.maxdisp/3))
        #added by CCJ:
        self.cv = cost_volume_faster # less memory and faster implement;
        
        #added for DFN
        self.isDFN = isDFN # True of False
        if self.isDFN:
            """ dynamic filter network """
            print(' Enable Dynamic Filter Network!!!')
            self.dfn_generator = filterGenerator(F = 32, 
                    dynamic_filter_size=(kernel_size, kernel_size), 
                    #img_size = (crop_img_h//4, crop_img_w//4), # due to 1/4 downsampling in PSMNet;
                    in_channels = 3,
                    #NOTE: Updated by CCJ on 2020/07/17, 3:47AM;
                    # net_init() function works well for all most the cases, except GANet, 
                    # due to sync BN used by GANet;
                    # So here instead, we will use net_init_SyncBN();
                    is_sync_bn=True,
                    )
            #the module layer:
            self.dfn_layer = DynamicFilterLayer(kernel_size, dilation)
        else:
            print(' Disable Dynamic Filter Network!!!')
        
        self.cost_filter_grad = cost_filter_grad

        for m in self.modules():
        #for idx, (n, m) in enumerate(self.named_modules()):
            #print ("[????] ganet init, idx ", idx, " - ", n)
            if isinstance(m, (nn.Conv2d, nn.Conv3d)):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (BatchNorm2d, BatchNorm3d)):
                #print ("[????] ganet init bn, idx ", idx, " - ", n)
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
    
    # comments added by CCJ:
    # x : left image;
    # y : right image;
    def forward(self, x, y):
        #added for DFN:
        # downscale x to [N,C,H/3, W/3] then fed into embeddingnet,
        # because the cost volume generated below is in shape [N,C,D/3, H/3, W/3]
        if self.isDFN:
            x_scale = F.interpolate(x, [x.size()[2]//3, x.size()[3]//3], 
                        mode='bilinear', align_corners=False)
            dfn_filter, dfn_bias = self.dfn_generator(x_scale) 
        
        g = self.conv_start(x)	
        x = self.feature(x)
        

        rem = x 
        x = self.conv_x(x)

        y = self.feature(y)
        y = self.conv_y(y)
        # feture concatenation to generate cost volume
        #x = self.cv(x,y)
        #added by CCJ:
        x = self.cv(x,y, self.maxdisp//3 +1) #NOTE: +1 is following the func GetCostVolume(), TODO: double check it;

        x1 = self.conv_refine(rem)
        x1 = F.interpolate(x1, 
                #NOTE: 
                # comments added by CCJ on 2019/10/14:
                # if input has shape [N, C, H,W], then the size for interpolation is
                # in this order: size=[size_H, size_W]
                # Note that the interpolation do not change the dim = N and C !!!
                [x1.size()[2]*3,x1.size()[3]*3], 
                mode='bilinear', align_corners=False)
        x1 = self.bn_relu(x1)
        g = torch.cat((g, x1), 1)
        g = self.guidance(g)
        

        #N, C, D, H, W = cv.size()[:]
        if self.isDFN:
            #print ('[???] cv shape', x.shape, "dfn_filter&dfn_bias device = ", dfn_filter.get_device(), dfn_bias.get_device())
            D = x.size()[2]
            with torch.set_grad_enabled(self.cost_filter_grad):
                for d in range(0,D):
                    #print ('bilateral filtering cost volume slice %d/%d' %(d+1, D))
                    cv_d_slice = x[:,:,d,:,:].contiguous()
                    x[:,:,d,:,:] = self.dfn_layer(cv_d_slice, dfn_filter, dfn_bias)
            
            # make sure the contiguous memeory
            x = x.contiguous()
            #print ('[???] done dfn(x), cost volume device = ', x.get_device())

        if self.training:
            disp0, disp1, disp2 = self.cost_agg(x, g)
            return disp0, disp1, disp2
        else:
            return self.cost_agg(x, g)
