import torch
import torch._utils
import torch.nn as nn
import torch.nn.functional as F
from torch.hub import load_state_dict_from_url

BN_MOMENTUM = 0.1


def conv3x3(in_planes, out_planes, stride=1):
    return nn.Conv2d(in_channels=in_planes, out_channels=out_planes, kernel_size=3,
                     stride=stride, padding=1, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(num_features=planes, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(num_features=planes, momentum=BN_MOMENTUM)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        # residual模块 shortcut分支是否需要下采样
        if self.downsample is not None:
            residual = self.downsample(x)

        out = out + residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(in_channels=inplanes, out_channels=planes, kernel_size=1, bias=False)  # channels: inplanes -> planes
        self.bn1 = nn.BatchNorm2d(num_features=planes, momentum=BN_MOMENTUM)
        self.conv2 = nn.Conv2d(in_channels=planes, out_channels=planes, kernel_size=3,  # channels: planes -> planes
                               stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(num_features=planes, momentum=BN_MOMENTUM)
        self.conv3 = nn.Conv2d(in_channels=planes, out_channels=planes * self.expansion,  # channels: planes -> planes * 4
                               kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(num_features=planes * self.expansion, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        # residual模块 shortcut分支是否需要下采样
        if self.downsample is not None:
            residual = self.downsample(x)

        out = out + residual
        out = self.relu(out)
        return out


class HighResolutionModule(nn.Module):
    def __init__(self, num_branches, blocks, num_blocks, num_inchannels,
                 num_channels, multi_scale_output=True):
        super(HighResolutionModule, self).__init__()
        self.num_inchannels = num_inchannels
        self.num_branches = num_branches

        self.multi_scale_output = multi_scale_output

        self.branches = self._make_branches(num_branches, blocks, num_blocks, num_channels)  # BasicBlock * 4, BasicBlock * 4
        self.fuse_layers = self._make_fuse_layers()
        self.relu = nn.ReLU(inplace=True)

    def _make_one_branch(self, branch_index, block, num_blocks, num_channels, stride=1):
        downsample = None
        if stride != 1 or self.num_inchannels[branch_index] != num_channels[branch_index] * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(in_channels=self.num_inchannels[branch_index], out_channels=num_channels[branch_index] * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(num_features=num_channels[branch_index] * block.expansion, momentum=BN_MOMENTUM),
            )

        layers = []
        layers.append(block(inplanes=self.num_inchannels[branch_index], planes=num_channels[branch_index], stride=stride, downsample=downsample))
        self.num_inchannels[branch_index] = num_channels[branch_index] * block.expansion  # 更新inchannels的大小
        for i in range(1, num_blocks[branch_index]):
            layers.append(block(inplanes=self.num_inchannels[branch_index], planes=num_channels[branch_index]))  # 重复堆叠block

        return nn.Sequential(*layers)

    def _make_branches(self, num_branches, block, num_blocks, num_channels):
        branches = []

        for branch_index in range(num_branches):
            branches.append(self._make_one_branch(branch_index, block, num_blocks, num_channels))

        return nn.ModuleList(branches)

    def _make_fuse_layers(self):
        if self.num_branches == 1:
            return None

        fuse_layers = []
        for i in range(self.num_branches if self.multi_scale_output else 1):
            fuse_layer = []
            for j in range(self.num_branches):
                if j > i:
                    fuse_layer.append(nn.Sequential(
                        nn.Conv2d(in_channels=self.num_inchannels[j], out_channels=self.num_inchannels[i], kernel_size=1, stride=1, padding=0, bias=False),
                        nn.BatchNorm2d(self.num_inchannels[i], momentum=BN_MOMENTUM)))
                elif j == i:
                    fuse_layer.append(None)
                else:
                    conv3x3s = []
                    for k in range(i - j):
                        if k == i - j - 1:
                            conv3x3s.append(
                                nn.Sequential(
                                    nn.Conv2d(in_channels=self.num_inchannels[j], out_channels=self.num_inchannels[i], kernel_size=3, stride=2, padding=1, bias=False),
                                    nn.BatchNorm2d(num_features=self.num_inchannels[i], momentum=BN_MOMENTUM)
                                )
                            )
                        else:
                            conv3x3s.append(
                                nn.Sequential(
                                    nn.Conv2d(self.num_inchannels[j], self.num_inchannels[j], 3, 2, 1, bias=False),
                                    nn.BatchNorm2d(self.num_inchannels[j], momentum=BN_MOMENTUM),
                                    nn.ReLU(inplace=True)
                                )
                            )
                    fuse_layer.append(nn.Sequential(*conv3x3s))
            fuse_layers.append(nn.ModuleList(fuse_layer))

        return nn.ModuleList(fuse_layers)

    def get_num_inchannels(self):
        return self.num_inchannels

    def forward(self, x):  # x0(bs, 32, 120, 120), x1(bs, 64, 60, 60)
        if self.num_branches == 1:
            return [self.branches[0](x[0])]

        for i in range(self.num_branches):
            x[i] = self.branches[i](x[i])  # x0(bs, 32, 120, 120), x1(bs, 64, 60, 60)

        x_fuse = []
        for i in range(len(self.fuse_layers)):
            y = 0
            # y = x[0] if i == 0 else self.fuse_layers[i][0](x[0])
            for j in range(0, self.num_branches):
                if j > i:
                    width_output = x[i].shape[-1]
                    height_output = x[i].shape[-2]
                    y = y + F.interpolate(
                        self.fuse_layers[i][j](x[j]),
                        size=[height_output, width_output],
                        mode='bilinear', align_corners=True
                    )
                elif i == j:
                    y = y + x[j]
                else:
                    y = y + self.fuse_layers[i][j](x[j])
            x_fuse.append(self.relu(y))

        return x_fuse


class HighResolutionNet_Classification(nn.Module):
    def __init__(self, num_classes, backbone):
        super(HighResolutionNet_Classification, self).__init__()
        num_filters = {
            'hrnetv2_w18': [18, 36, 72, 144],
            'hrnetv2_w32': [32, 64, 128, 256],
            'hrnetv2_w48': [48, 96, 192, 384],
        }[backbone]
        # stem net
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=64, kernel_size=3, stride=2, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(num_features=64, momentum=BN_MOMENTUM)
        self.conv2 = nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=2, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(num_features=64, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)

        self.layer1 = self._make_layer(block=Bottleneck, inplanes=64, planes=64, num_blocks=4)  # bottleneck x 4

        pre_stage_channels = [Bottleneck.expansion * 64]  # pre_stage_channels = 4 * 64
        num_channels = [num_filters[0], num_filters[1]]  # num_channels = [32, 64]
        self.transition1 = self._make_transition_layer(num_inchannels=pre_stage_channels, num_channels=num_channels)  # _make_transition_layer([256], [32, 64])
        self.stage2, pre_stage_channels = self._make_stage(num_modules=1, num_branches=2, block=BasicBlock,
                                                           num_blocks=[4, 4], num_inchannels=num_channels,
                                                           num_channels=num_channels)  # num_channels=[32, 64],

        num_channels = [num_filters[0], num_filters[1], num_filters[2]]  # num_channels=[32, 64, 128]
        self.transition2 = self._make_transition_layer(num_inchannels=pre_stage_channels, num_channels=num_channels)  # pre_stage_channels = [32, 64]
        self.stage3, pre_stage_channels = self._make_stage(num_modules=4, num_branches=3, block=BasicBlock,
                                                           num_blocks=[4, 4, 4], num_inchannels=num_channels,
                                                           num_channels=num_channels)

        num_channels = [num_filters[0], num_filters[1], num_filters[2], num_filters[3]]  # num_channels = [32, 64, 128, 256]
        self.transition3 = self._make_transition_layer(pre_stage_channels, num_channels)  # pre_stage_channels = [32, 64, 128]
        self.stage4, pre_stage_channels = self._make_stage(num_modules=3, num_branches=4, block=BasicBlock,
                                                           num_blocks=[4, 4, 4, 4], num_inchannels=num_channels,
                                                           num_channels=num_channels)

        self.pre_stage_channels = pre_stage_channels  # pre_stage_channels = [32, 64, 128, 256]

        self.incre_modules, self.downsamp_modules, self.final_layer = self._make_head(block=Bottleneck,
                                                                                      pre_stage_channels=pre_stage_channels)

        self.classifier = nn.Linear(in_features=2048, out_features=num_classes)

    def _make_layer(self, block, inplanes, planes, num_blocks, stride=1):
        downsample = None
        if stride != 1 or inplanes != planes * block.expansion:  # bottleneck.expansion=4, basicblock.expansion=1
            downsample = nn.Sequential(
                nn.Conv2d(in_channels=inplanes, out_channels=planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(num_features=planes * block.expansion,
                               momentum=BN_MOMENTUM),
            )

        layers = []
        layers.append(block(inplanes, planes, stride, downsample))  # block: bottleneck / basicblock
        inplanes = planes * block.expansion
        for i in range(1, num_blocks):
            layers.append(block(inplanes, planes))

        return nn.Sequential(*layers)

    def _make_transition_layer(self, num_inchannels, num_channels):
        num_branches_pre = len(num_inchannels)
        num_branches_cur = len(num_channels)

        transition_layers = []
        for i in range(num_branches_cur):
            if i < num_branches_pre:
                if num_channels[i] != num_inchannels[i]:
                    transition_layers.append(nn.Sequential(
                        nn.Conv2d(in_channels=num_inchannels[i], out_channels=num_channels[i], kernel_size=3, stride=1, padding=1, bias=False),
                        nn.BatchNorm2d(num_features=num_channels[i], momentum=BN_MOMENTUM),
                        nn.ReLU(inplace=True)
                    ))
                else:
                    transition_layers.append(None)
            else:
                conv3x3s = [
                    nn.Sequential(  # Conv3x3 s2 p1增加一个分支
                        nn.Conv2d(in_channels=num_inchannels[-1], out_channels=num_channels[i], kernel_size=3, stride=2, padding=1, bias=False),
                        nn.BatchNorm2d(num_channels[i], momentum=BN_MOMENTUM),
                        nn.ReLU(inplace=True)
                    )
                ]
                transition_layers.append(nn.Sequential(*conv3x3s))

        return nn.ModuleList(transition_layers)

    def _make_stage(self, num_modules, num_branches, block, num_blocks, num_inchannels, num_channels,
                    multi_scale_output=True):
        modules = []
        for i in range(num_modules):
            modules.append(
                HighResolutionModule(num_branches, block, num_blocks, num_inchannels, num_channels, multi_scale_output)  # 不同尺寸的feature maps融合模块
            )
            num_inchannels = modules[-1].get_num_inchannels()

        return nn.Sequential(*modules), num_inchannels

    def _make_head(self, block, pre_stage_channels):
        head_channels = [32, 64, 128, 256]

        incre_modules = []
        for i, channels in enumerate(pre_stage_channels):  # pre_stage_channels=[32, 64, 128, 256]
            incre_module = self._make_layer(block=block, inplanes=channels,  # block=BottleNeck
                                            planes=head_channels[i], num_blocks=1, stride=1)
            incre_modules.append(incre_module)
        incre_modules = nn.ModuleList(incre_modules)

        downsamp_modules = []
        for i in range(len(pre_stage_channels) - 1):
            in_channels = head_channels[i] * block.expansion
            out_channels = head_channels[i + 1] * block.expansion

            downsamp_module = nn.Sequential(
                nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=3, stride=2, padding=1),
                nn.BatchNorm2d(num_features=out_channels, momentum=BN_MOMENTUM),
                nn.ReLU(inplace=True)
            )

            downsamp_modules.append(downsamp_module)
        downsamp_modules = nn.ModuleList(downsamp_modules)

        final_layer = nn.Sequential(
            nn.Conv2d(
                in_channels=head_channels[3] * block.expansion,
                out_channels=2048,
                kernel_size=1,
                stride=1,
                padding=0
            ),
            nn.BatchNorm2d(num_features=2048, momentum=BN_MOMENTUM),
            nn.ReLU(inplace=True)
        )

        return incre_modules, downsamp_modules, final_layer

    def forward(self, x):
        # ------ module1 ------ Conv3x3 + Conv3x3 + layer1
        x = self.conv1(x)  # x()
        x = self.bn1(x)
        x = self.relu(x)
        x = self.conv2(x)
        x = self.bn2(x)
        x = self.relu(x)
        x = self.layer1(x)
        # ------ module2 ------ Transition1 + Stage2
        x_list = []
        for i in range(2):
            if self.transition1[i] is not None:
                x_list.append(self.transition1[i](x))
            else:
                x_list.append(x)
        y_list = self.stage2(x_list)
        # ------ module3 ------ Transition2 + Stage3
        x_list = []
        for i in range(3):
            if self.transition2[i] is not None:
                if i < 2:
                    x_list.append(self.transition2[i](y_list[i]))
                else:
                    x_list.append(self.transition2[i](y_list[-1]))
            else:
                x_list.append(y_list[i])
        y_list = self.stage3(x_list)
        # ------ module4 ------ Transition3 + Stage4
        x_list = []
        for i in range(4):
            if self.transition3[i] is not None:
                if i < 3:
                    x_list.append(self.transition3[i](y_list[i]))
                else:
                    x_list.append(self.transition3[i](y_list[-1]))
            else:
                x_list.append(y_list[i])
        y_list = self.stage4(x_list)

        # ------ incre_modules + downsamp_modules + final_layer (delete)------
        y = self.incre_modules[0](y_list[0])
        for i in range(len(self.downsamp_modules)):
            y = self.incre_modules[i + 1](y_list[i + 1]) + \
                self.downsamp_modules[i](y)

        y = self.final_layer(y)

        if torch._C._get_tracing_state():
            y = y.flatten(start_dim=2).mean(dim=2)
        else:
            y = F.avg_pool2d(y, kernel_size=y.size()
            [2:]).view(y.size(0), -1)

        y = self.classifier(y)

        return y


def hrnet_classification(pretrained=False, backbone='hrnetv2_w18'):
    model = HighResolutionNet_Classification(num_classes=1000, backbone=backbone)
    if pretrained:
        model_urls = {
            'hrnetv2_w18': "https://github.com/bubbliiiing/hrnet-pytorch/releases/download/v1.0/hrnetv2_w18_imagenet_pretrained.pth",
            'hrnetv2_w32': "https://github.com/bubbliiiing/hrnet-pytorch/releases/download/v1.0/hrnetv2_w32_imagenet_pretrained.pth",
            'hrnetv2_w48': "https://github.com/bubbliiiing/hrnet-pytorch/releases/download/v1.0/hrnetv2_w48_imagenet_pretrained.pth",
        }
        state_dict = load_state_dict_from_url(url=model_urls[backbone], model_dir="./model_data")
        model.load_state_dict(state_dict)

    return model
