import torch
import torch.nn as nn
import torch.nn.functional as F
from torchsummary import summary
from thop import profile

# class model(nn.Module):

# 3x3 convolutional module
def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=dilation, groups=groups, bias=False, dilation=dilation)

# 1x1 convolutional module
def conv1x1(in_planes, out_planes, stride=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)

# transformer module positional embeddings fill with [-1,1]
def position(H, W, is_cuda=True):
    if is_cuda:
        loc_w = torch.linspace(-1.0, 1.0, W).cuda().unsqueeze(0).repeat(H, 1)
        loc_h = torch.linspace(-1.0, 1.0, H).cuda().unsqueeze(1).repeat(1, W)
    else:
        loc_w = torch.linspace(-1.0, 1.0, W).unsqueeze(0).repeat(H, 1)
        loc_h = torch.linspace(-1.0, 1.0, H).unsqueeze(1).repeat(1, W)
    print(loc_w.shape)
    print(loc_h.shape)
    loc = torch.cat([loc_w.unsqueeze(0), loc_h.unsqueeze(0)], 0).unsqueeze(0)
    print(loc.shape)
    return loc

def position_3D(C, H, W, is_cuda=True):
    if is_cuda:
        loc_w = torch.linspace(-1.0, 1.0, W).cuda().unsqueeze(0).repeat(H, 1).repeat(C, 1, 1)
        loc_h = torch.linspace(-1.0, 1.0, H).cuda().unsqueeze(1).repeat(1, W).repeat(C, 1, 1)
    else:
        loc_w = torch.linspace(-1.0, 1.0, W).cuda().unsqueeze(0).repeat(H, 1).repeat(C, 1, 1)
        loc_h = torch.linspace(-1.0, 1.0, H).cuda().unsqueeze(1).repeat(1, W).repeat(C, 1, 1)
    loc = torch.cat([loc_w.unsqueeze(0), loc_h.unsqueeze(0)], 0).unsqueeze(0)
    return loc

def stride(x, stride):
    b, c, h, w = x.shape
    return x[:, :, ::stride, ::stride]

def init_rate_half(tensor):
    if tensor is not None:
        tensor.data.fill_(0.5)

def init_rate_0(tensor):
    if tensor is not None:
        tensor.data.fill_(0.)

# ACmix module
class ACmix(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_att=7, head=4, kernel_conv=3, stride=1, dilation=1):
        super(ACmix, self).__init__()
        self.in_planes = in_planes
        self.out_planes = out_planes
        self.head = head
        self.kernel_att = kernel_att
        self.kernel_conv = kernel_conv
        self.stride = stride
        self.dilation = dilation
        self.rate1 = torch.nn.Parameter(torch.Tensor(1))
        self.rate2 = torch.nn.Parameter(torch.Tensor(1))
        self.head_dim = self.out_planes // self.head

        self.conv1 = nn.Conv2d(in_planes, out_planes, kernel_size=1)
        self.conv1_3D = nn.Conv3d(in_planes, out_planes, kernel_size=(1, 1, 1))

        self.conv2 = nn.Conv2d(in_planes, out_planes, kernel_size=1)
        self.conv2_3D = nn.Conv3d(in_planes, out_planes, kernel_size=(1, 1, 1))

        self.conv3 = nn.Conv2d(in_planes, out_planes, kernel_size=1)
        self.conv3_3D = nn.Conv3d(in_planes, out_planes, kernel_size=(1, 1, 1))

        self.conv_p = nn.Conv2d(2, self.head_dim, kernel_size=1)
        self.conv_p_3D = nn.Conv3d(2, self.head_dim, kernel_size=(1, 1, 1))

        self.padding_att = (self.dilation * (self.kernel_att - 1) + 1) // 2

        self.pad_att = torch.nn.ReflectionPad2d(self.padding_att)
        self.pad_att_3D = torch.nn.ReflectionPad3d((self.padding_att,self.padding_att,self.padding_att,self.padding_att,0,0))

        self.unfold = nn.Unfold(kernel_size=self.kernel_att, padding=0, stride=self.stride)
        self.softmax = torch.nn.Softmax(dim=1)

        self.fc = nn.Conv2d(3*self.head, self.kernel_conv * self.kernel_conv, kernel_size=1, bias=False)
        self.fc_3D = nn.Conv3d(3*self.head, self.kernel_conv * self.kernel_conv, kernel_size=(1, 1, 1), bias=False)

        self.dep_conv = nn.Conv2d(self.kernel_conv * self.kernel_conv * self.head_dim, out_planes, kernel_size=self.kernel_conv, bias=True, groups=self.head_dim, padding=1, stride=stride)
        self.dep_conv_3D = nn.Conv3d(self.kernel_conv * self.kernel_conv * self.head_dim, out_planes, kernel_size=self.kernel_conv, bias=True, groups=self.head_dim, padding=1, stride=stride)

        self.reset_parameters()
    
    def reset_parameters(self):
        init_rate_half(self.rate1)
        init_rate_half(self.rate2)
        kernel = torch.zeros(self.kernel_conv * self.kernel_conv, self.kernel_conv, self.kernel_conv)
        for i in range(self.kernel_conv * self.kernel_conv):
            kernel[i, i//self.kernel_conv, i%self.kernel_conv] = 1.
        kernel = kernel.squeeze(0).repeat(self.out_planes, 1, 1, 1)
        self.dep_conv.weight = nn.Parameter(data=kernel, requires_grad=True)
        self.dep_conv.bias = init_rate_0(self.dep_conv.bias)

    def forward(self, x):

        print("xshape")
        print(x.shape)

        q, k, v = self.conv1_3D(x), self.conv2_3D(x), self.conv3_3D(x)
        scaling = float(self.head_dim) ** -0.5
        b, l, c, h, w = q.shape

        print("qshape")
        print(q.shape)

        h_out, w_out = h//self.stride, w//self.stride

        # ## positional encoding
        pe = self.conv_p_3D(position_3D(c, h, w, x.is_cuda))

        print("peshape")
        print(pe.shape)

        q_att = q.view(b*self.head, self.head_dim, c, h, w) * scaling
        k_att = k.view(b*self.head, self.head_dim, c, h, w)
        v_att = v.view(b*self.head, self.head_dim, c, h, w)

        print("q_attshape")
        print(q_att.shape)

        if self.stride > 1:
            q_att = stride(q_att, self.stride)
            q_pe = stride(pe, self.stride)
        else:
            q_pe = pe

        print("qpeshape")
        print(q_pe.shape)



        unfold_temp = self.pad_att_3D(k_att)
        print(unfold_temp.shape)
        # print(unfold_temp[:,:,1,:,:].shape)
        unfold_k = torch.tensor([]).cuda()
        for i in range (unfold_temp.shape[2]):
            unfold_k_temp = self.unfold(unfold_temp[:,:,i,:,:]).unsqueeze(0)
            # print(unfold_k_temp.shape)
            unfold_k = torch.concat([unfold_k, unfold_k_temp], 0)
        print(unfold_k.shape)
        # unfold_k = self.unfold(self.pad_att_3D(k_att)).view(b*self.head, self.head_dim, self.kernel_att*self.kernel_att, h_out, w_out) # b*head, head_dim, k_att^2, h_out, w_out

        unfold_temp = self.pad_att_3D(pe)
        # print(unfold_temp.shape)
        # print(unfold_temp[:,:,1,:,:].shape)
        unfold_rpe = torch.tensor([]).cuda()
        for i in range (unfold_temp.shape[2]):
            unfold_rpe_temp = self.unfold(unfold_temp[:,:,i,:,:]).unsqueeze(0)
            # print(unfold_rpe_temp.shape)
            unfold_rpe = torch.concat([unfold_rpe, unfold_rpe_temp], 0)
        print(unfold_rpe.shape)

        c_out = unfold_temp.shape[2]//self.stride

        unfold_k = unfold_k.view(b*self.head, self.head_dim, self.kernel_att*self.kernel_att, c_out, h_out, w_out)
        unfold_rpe = unfold_rpe.view(1, self.head_dim, self.kernel_att*self.kernel_att, c_out, h_out, w_out)
        # unfold_rpe = self.unfold(self.pad_att_3D(pe))
        # unfold_rpe = self.unfold(self.pad_att(pe)).view(1, self.head_dim, self.kernel_att*self.kernel_att, h_out, w_out) # 1, head_dim, k_att^2, h_out, w_out
        
        print("unfold_kshape")
        print(unfold_k.shape)
        print("unfold_rpeshape")
        print(unfold_rpe.shape)

        att = (q_att.unsqueeze(2)*(unfold_k + q_pe.unsqueeze(2) - unfold_rpe)).sum(1) # (b*head, head_dim, 1, h_out, w_out) * (b*head, head_dim, k_att^2, h_out, w_out) -> (b*head, k_att^2, h_out, w_out)
        
        print("attshape")
        print(att.shape)

        att = self.softmax(att)

        print("att_softmaxshape")
        print(att.shape)

        unfold_temp = self.pad_att_3D(v_att)
        # print(unfold_temp[:,:,1,:,:].shape)
        unfold_v = torch.tensor([]).cuda()
        for i in range (unfold_temp.shape[2]):
            unfold_v_temp = self.unfold(unfold_temp[:,:,i,:,:]).unsqueeze(0)
            # print(unfold_k_temp.shape)
            unfold_v = torch.concat([unfold_v, unfold_v_temp], 0)

        out_att = unfold_v.view(b*self.head, self.head_dim, self.kernel_att*self.kernel_att, c_out, h_out, w_out)
        
        print("out_attshape")
        print(out_att.shape)

        out_att = (att.unsqueeze(1) * out_att).sum(2).view(b, self.out_planes, c_out, h_out, w_out)

        print("out_attshape_last")
        print(out_att.shape)

        print("conv_before shape")
        print(torch.cat([q.view(b, self.head, self.head_dim, c, h*w), k.view(b, self.head, self.head_dim, c, h*w), v.view(b, self.head, self.head_dim, c, h*w)], 1).shape)

        f_all = self.fc_3D(torch.cat([q.view(b, self.head, self.head_dim, c, h*w), k.view(b, self.head, self.head_dim, c, h*w), v.view(b, self.head, self.head_dim, c, h*w)], 1))
        
        print("f_allshape")
        print(f_all.shape)
        
        f_conv = f_all.permute(0, 2, 1, 3, 4).reshape(x.shape[0], -1, x.shape[-3], x.shape[-2], x.shape[-1])
        # print(f_all.permute(0, 2, 1, 3).shape)
        # print(x.shape[0],x.shape[-2],x.shape[-1])
        print("f_convshape")
        print(f_conv.shape)

        out_conv = self.dep_conv_3D(f_conv)

        print("out_convshape")
        print(out_conv.shape)

        # print("result")
        # print((self.rate1 * out_att + self.rate2 * out_conv).shape)
        return self.rate1 * out_att + self.rate2 * out_conv
    
class Bottleneck(nn.Module):
    # Bottleneck in torchvision places the stride for downsampling at 3x3 convolution(self.conv2)
    # while original implementation places the stride at the first 1x1 convolution(self.conv1)
    # according to "Deep residual learning for image recognition"https://arxiv.org/abs/1512.03385.
    # This variant is also known as ResNet V1.5 and improves accuracy according to
    # https://ngc.nvidia.com/catalog/model-scripts/nvidia:resnet_50_v1_5_for_pytorch.

    expansion = 4

    def __init__(self, inplanes, planes, k_att, head, k_conv, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, norm_layer=None):
        super(Bottleneck, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        width = int(planes * (base_width / 64.)) * groups
        # Both self.conv2 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv1x1(inplanes, width)
        self.conv1_3D = nn.Conv3d(inplanes, width, kernel_size=(1, 1, 1))

        norm_layer = nn.BatchNorm3d

        self.bn1 = norm_layer(width)
        self.conv2 = ACmix(width, width, k_att, head, k_conv, stride=stride, dilation=dilation)

        self.bn2 = norm_layer(width)

        self.conv3 = conv1x1(width, planes * self.expansion)
        self.conv3_2D = nn.Conv3d(width, planes * self.expansion, kernel_size=(1, 1, 1))

        self.bn3 = norm_layer(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x

        out = self.conv1_3D(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3_2D(out)
        out = self.bn3(out)
        print("!!!!!!")
        print(out.shape)
        print(identity.shape)
        if self.downsample is not None:
           identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out


class ACmix_3D(nn.Module):

    def __init__(self, block=Bottleneck, layers=[1,1], input_channels=32, k_att=7, head=4, k_conv=3, num_classes=1000,
                 groups=1, width_per_group=64, replace_stride_with_dilation=None):
        super(ACmix_3D, self).__init__()

        self.inplanes = 64
        self.dilation = 1
        if replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            replace_stride_with_dilation = [False, False, False]
        if len(replace_stride_with_dilation) != 3:
            raise ValueError("replace_stride_with_dilation should be None "
                             "or a 3-element tuple, got {}".format(replace_stride_with_dilation))
        self.groups = groups
        self.base_width = width_per_group
        # self.conv1 = nn.Conv2d(input_channels, self.inplanes, kernel_size=7, stride=2, padding=3,
        #                        bias=False)
        
        self.conv1 = nn.Conv2d(input_channels, self.inplanes, kernel_size=7, stride=1, padding=3, bias=False)
        self.conv1_3D = nn.Conv3d(1, self.inplanes, kernel_size=(7, 7, 7), stride=(1, 1, 1), padding=(3, 3, 3))

        self.bn1 = nn.BatchNorm2d(self.inplanes)
        self.bn1_3D = nn.BatchNorm3d(self.inplanes)

        self.relu = nn.ReLU(inplace=True)
        self.relu_3D = nn.ReLU(inplace=True)

        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.maxpool_3D = nn.MaxPool3d(kernel_size=(3, 3, 3), stride=(2, 2, 2), padding=(1, 1, 1))


        self.layer1 = self.make_layer(block, 64, layers[0], k_att, head, k_conv)
        self.layer2 = self.make_layer(block, 128, layers[1], k_att, head, k_conv, stride=2,
                                       dilate=replace_stride_with_dilation[0])
        # self.layer3 = self._make_layer(block, 256, layers[2], k_att, head, k_conv, stride=2,
        #                                dilate=replace_stride_with_dilation[1])
        # self.layer4 = self._make_layer(block, 512, layers[3], k_att, head, k_conv, stride=2,
        #                                dilate=replace_stride_with_dilation[2])

        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.avgpool_3D = nn.AdaptiveAvgPool3d((1, 1, 1))

        self.fc = nn.Linear(64 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
        
        for m in self.modules():
            if isinstance(m, nn.Conv3d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm3d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def make_layer(self, block, planes, blocks, rate, k, head, stride=1, dilate=False):
        norm_layer = nn.BatchNorm3d
        downsample = None
        previous_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                # conv1x1(self.inplanes, planes * block.expansion, stride),
                nn.Conv3d(self.inplanes, planes * block.expansion, stride),
                norm_layer(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, rate, k, head, stride, downsample, self.groups,
                            self.base_width, previous_dilation, norm_layer))
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes, rate, k, head, groups=self.groups,
                                base_width=self.base_width, dilation=self.dilation,
                                norm_layer=norm_layer))

        return nn.Sequential(*layers)

    def forward(self, x):
        # See note [TorchScript super()]
        print("1")
        print(x.shape)
        
        # x = x.unsqueeze(1)
        print("2")
        print(x.shape)

        x = self.conv1_3D(x)
        print("3")
        print(x.shape)

        x = self.bn1_3D(x)
        print("4")
        print(x.shape)

        x = self.relu_3D(x)
        print("5")
        print(x.shape)

        x = self.maxpool_3D(x)
        print("6")
        print(x.shape)

        # print("layer1")
        x = self.layer1(x)
        print("7")
        print(x.shape)
        # print("layer2")
        # x = self.layer2(x)
        # print("layer3")
        # x = self.layer3(x)
        # print("layer4")
        # x = self.layer4(x)

        x = self.avgpool_3D(x)
        print("8")
        print(x.shape)

        x = torch.flatten(x, 1)
        print("9")
        print(x.shape)
        
        x = self.fc(x)
        print("10")
        print(x.shape)

        return x


if __name__ == '__main__':
    model = ACmix_3D().cuda()
    input = torch.randn([2,1,32,8,8]).cuda()
    total_params = sum(p.numel() for p in model.parameters())
    print(f'{total_params:,} total parameters.')
    total_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f'{total_trainable_params:,} training parameters.')
    print(model(input).shape)
    flops, params = profile(model, inputs=(input,))
    print("flops:{:.3f}G".format(flops / 1e9))
    print("params:{:.3f}M".format(params / 1e6))
    # --------------------------------------------------#
    #   用来测试网络能否跑通，同时可查看FLOPs和params
    # --------------------------------------------------#
    summary(model, input_size=(1,32,8,8), batch_size=-1)