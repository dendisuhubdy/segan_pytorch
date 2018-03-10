import torch
from torch.autograd import Variable
import torch.nn.functional as F
import torch.nn as nn
import numpy as np
try:
    from core import Model, LayerNorm
except ImportError:
    from .core import Model, LayerNorm

class GBlock(nn.Module):

    def __init__(self, ninputs, fmaps, kwidth,
                 activation, padding=None,
                 lnorm=False, dropout=0.,
                 pooling=2, enc=True, bias=False,
                 aal_h=None, linterp=False):
        # linterp: do linear interpolation instead of simple conv transpose
        super().__init__()
        self.pooling = pooling
        self.linterp = linterp
        if padding is None:
            padding = (kwidth // 2)
        if enc:
            if aal_h is not None:
                self.aal_conv = nn.Conv1d(ninputs, ninputs, 
                                          aal_h.shape[0],
                                          stride=1,
                                          padding=aal_h.shape[0] // 2 - 1,
                                          bias=False)
                # apply AAL weights, reshaping impulse response to match
                # in channels and out channels
                aal_t = torch.FloatTensor(aal_h).view(1, 1, -1)
                aal_t = aal_t.repeat(ninputs, ninputs, 1)
                self.aal_conv.weight.data = aal_t
            self.conv = nn.Conv1d(ninputs, fmaps, kwidth,
                                  stride=pooling,
                                  padding=padding,
                                  bias=bias)
            if activation == 'glu':
                self.glu_conv = nn.Conv1d(ninputs, fmaps, kwidth,
                                          stride=pooling,
                                          padding=padding,
                                          bias=bias)
        else:
            if linterp:
                self.conv = nn.Conv1d(ninputs, fmaps, kwidth-1,
                                      stride=1, padding=(kwidth-1)//2,
                                      bias=bias)
                if activation == 'glu':
                    self.glu_conv = nn.Conv1d(ninputs, fmaps, kwidth-1,
                                              stride=1, padding=(kwidth-1)//2,
                                              bias=bias)
            else:
                # decoder like with transposed conv
                self.conv = nn.ConvTranspose1d(ninputs, fmaps, kwidth,
                                               stride=pooling,
                                               padding=padding,
                                               bias=bias)
                if activation == 'glu':
                    self.glu_conv = nn.ConvTranspose1d(ninputs, fmaps, kwidth,
                                                       stride=pooling,
                                                       padding=padding,
                                                       bias=bias)
        if activation is not None:
            self.act = activation
        if lnorm:
            self.ln = LayerNorm()
        if dropout > 0:
            self.dout = nn.Dropout(dropout)

    def forward(self, x):
        if len(x.size()) == 4:
            # inverse case from 1D -> 2D, go 2D -> 1D
            # re-format input from [B, K, C, L] to [B, K * C, L]
            # where C: frequency, L: time
            x = x.squeeze(1)
        if hasattr(self, 'aal_conv'):
            x = self.aal_conv(x)
        if self.linterp:
            x = F.upsample(x, scale_factor=self.pooling,
                           mode='linear')
        h = self.conv(x)
        if hasattr(self, 'act'):
            if self.act == 'glu':
                hg = self.glu_conv(x)
                h = h * F.sigmoid(hg)
            else:
                h = self.act(h)
        if hasattr(self, 'ln'):
            h = self.ln(h)
        if hasattr(self, 'dout'):
            h = self.dout(h)
        return h


class G2Block(nn.Module):
    """ Conv2D Generator Blocks """

    def __init__(self, ninputs, fmaps, kwidth,
                 activation, padding=None,
                 bnorm=False, dropout=0.,
                 pooling=2, enc=True, bias=False):
        super().__init__()
        if padding is None:
            padding = (kwidth // 2)
        if enc:
            self.conv = nn.Conv2d(ninputs, fmaps, kwidth,
                                  stride=pooling,
                                  padding=padding,
                                  bias=bias)
        else:
            # decoder like with transposed conv
            self.conv = nn.ConvTranspose2d(ninputs, fmaps, kwidth,
                                           stride=pooling,
                                           padding=padding)
        if bnorm:
            self.bn = nn.BatchNorm2d(fmaps)
        if activation is not None:
            self.act = activation
        if dropout > 0:
            self.dout = nn.Dropout2d(dropout)

    def forward(self, x):
        if len(x.size()) == 3:
            # re-format input from [B, C, L] to [B, 1, C, L]
            # where C: frequency, L: time
            x = x.unsqueeze(1)
        h = self.conv(x)
        if hasattr(self, 'bn'):
            h = self.bn(h)
        if hasattr(self, 'act'):
            h = self.act(h)
        if hasattr(self, 'dout'):
            h = self.dout(h)
        return h


class Generator1D(Model):

    def __init__(self, ninputs, enc_fmaps, kwidth,
                 activations, lnorm=False, dropout=0.,
                 pooling=2, z_dim=256, z_all=False,
                 skip=True, skip_blacklist=[],
                 dec_activations=None, cuda=False,
                 bias=False, aal=False, wd=0.,
                 skip_init='zero', skip_dropout=0.5,
                 no_tanh=False, aal_out=False,
                 rnn_core=False, linterp=False,
                 mlpconv=False, dec_kwidth=None,
                 subtract_mean=False, no_z=False):
        # subract_mean: from output signal, get rif of mean by windows
        super().__init__(name='Generator1D')
        self.dec_kwidth = dec_kwidth
        self.skip = skip
        self.skip_init = skip_init
        self.skip_dropout = skip_dropout
        self.subtract_mean = subtract_mean
        self.z_dim = z_dim
        self.z_all = z_all
        # do not place any z
        self.no_z = no_z
        self.do_cuda = cuda
        self.wd = wd
        self.no_tanh = no_tanh
        self.skip_blacklist = skip_blacklist
        self.gen_enc = nn.ModuleList()
        if aal or aal_out:
            # Make cheby1 filter to include into pytorch conv blocks
            from scipy.signal import cheby1, dlti, dimpulse
            system = dlti(*cheby1(8, 0.05, 0.8 / pooling))
            tout, yout = dimpulse(system)
            filter_h = yout[0]
        if aal:
            self.filter_h = filter_h
        else:
            self.filter_h = None

        if dec_kwidth is None:
            dec_kwidth = kwidth + 1

        if isinstance(activations, str):
            if activations != 'glu':
                activations = getattr(nn, activations)()
        if not isinstance(activations, list):
            activations = [activations] * len(enc_fmaps)
        
        skips = {}
        # Build Encoder
        for layer_idx, (fmaps, act) in enumerate(zip(enc_fmaps, 
                                                     activations)):
            if layer_idx == 0:
                inp = ninputs
            else:
                inp = enc_fmaps[layer_idx - 1]
            if self.skip and layer_idx < (len(enc_fmaps) - 1):
                if layer_idx not in self.skip_blacklist:
                    l_i = layer_idx
                    skips[l_i] = {'alpha':self.init_alpha(fmaps)}
                    setattr(self, 'alpha_{}'.format(l_i), skips[l_i]['alpha'])
                    if self.skip_dropout > 0:
                        skips[l_i]['dropout'] = nn.Dropout(self.skip_dropout)
            self.gen_enc.append(GBlock(inp, fmaps, kwidth, act,
                                       padding=None, lnorm=lnorm, 
                                       dropout=dropout, pooling=pooling,
                                       enc=True, bias=bias, 
                                       aal_h=self.filter_h))
        self.skips = skips
        dec_inp = enc_fmaps[-1]
        if mlpconv:
            dec_fmaps = enc_fmaps[::-1][1:] + [128, 64, 1] 
            up_poolings = [2] * (len(dec_fmaps) - 2) + [1] * 3
        else:
            dec_fmaps = enc_fmaps[::-1][1:] + [1]
            up_poolings = [2] * len(dec_fmaps)
        if rnn_core:
            self.z_all = False
            z_all = False
            # place a bidirectional RNN layer in the core to condition
            # everything to everything AND Z will be the init state of it
            self.rnn_core = nn.LSTM(dec_inp, dec_inp // 2, bidirectional=True,
                                    batch_first=True)
        else:
            if no_z:
                all_z = False
            else:
                dec_inp += z_dim
        #print(dec_fmaps)
        # Build Decoder
        self.gen_dec = nn.ModuleList()

        if dec_activations is None:
            # assign same activations as in Encoder
            dec_activations = [activations[0]] * len(dec_fmaps)
        
        enc_layer_idx = len(enc_fmaps) - 1
        for layer_idx, (fmaps, act) in enumerate(zip(dec_fmaps, 
                                                     dec_activations)):
            if skip and layer_idx > 0 and enc_layer_idx not in skip_blacklist:
                #print('Added skip conn input of enc idx: {} and size:'
                #      ' {}'.format(enc_layer_idx, dec_inp))
                pass

            if z_all and layer_idx > 0:
                dec_inp += z_dim

            if layer_idx >= len(dec_fmaps) - 1:
                if self.no_tanh:
                    act = None
                else:
                    act = nn.Tanh()
                lnorm = False
                dropout = 0
                dec_kwidth = 2
                kwidth = 2
            if up_poolings[layer_idx] > 1:
                self.gen_dec.append(GBlock(dec_inp,
                                           fmaps, dec_kwidth, act, 
                                           padding=(dec_kwidth//2) - 1, 
                                           lnorm=lnorm,
                                           dropout=dropout, pooling=pooling, 
                                           enc=False,
                                           bias=bias,
                                           linterp=linterp))
            else:
                self.gen_dec.append(GBlock(dec_inp,
                                           fmaps, kwidth, act, 
                                           lnorm=lnorm,
                                           dropout=dropout, pooling=1,
                                           enc=True,
                                           bias=bias))
            dec_inp = fmaps
        if aal_out:
            # make AAL filter to put in output
            self.aal_out = nn.Conv1d(1, 1, filter_h.shape[0] + 1,
                                     stride=1, 
                                     padding=filter_h.shape[0] // 2,
                                     bias=False)
            print('filter_h shape: ', filter_h.shape)
            # apply AAL weights, reshaping impulse response to match
            # in channels and out channels
            aal_t = torch.FloatTensor(filter_h).view(1, 1, -1)
            aal_t = torch.cat((aal_t, torch.zeros(1, 1, 1)), dim=-1)
            self.aal_out.weight.data = aal_t
            print('aal_t size: ', aal_t.size())


    def init_alpha(self, size):
        if self.skip_init == 'zero':
            alpha_ = torch.zeros(size)
        elif self.skip_init == 'randn':
            alpha_ = torch.randn(size)
        elif self.skip_init == 'one':
            alpha_ = torch.ones(size)
        else:
            raise TypeError('Unrecognized alpha init scheme: ', 
                            self.init_alpha)
        if self.do_cuda:
            alpha_ = alpha_.cuda()
        return nn.Parameter(alpha_)
        

    def forward(self, x, z=None, ret_hid=False):
        hall = {}
        hi = x
        skips = self.skips
        for l_i, enc_layer in enumerate(self.gen_enc):
            hi = enc_layer(hi)
            #print('ENC {} hi size: {}'.format(l_i, hi.size()))
                    #print('Adding skip[{}]={}, alpha={}'.format(l_i,
                    #                                            hi.size(),
                    #                                            hi.size(1)))
            if self.skip and l_i < (len(self.gen_enc) - 1):
                if l_i not in self.skip_blacklist:
                    skips[l_i]['tensor'] = hi
            if ret_hid:
                hall['enc_{}'.format(l_i)] = hi
        if hasattr(self, 'rnn_core'):
            self.z_all = False
            if z is None:
                # make z as initial RNN state forward and backward
                # (2 directions)
                if self.no_z:
                    # MAKE DETERMINISTIC ZERO
                    h0 = Variable(torch.zeros(2, hi.size(0), hi.size(1)//2))
                else:
                    h0 = Variable(torch.randn(2, hi.size(0), hi.size(1)//2))
                c0 = Variable(torch.zeros(2, hi.size(0), hi.size(1)//2))
                if self.do_cuda:
                    h0 = h0.cuda()
                    c0 = c0.cuda()
                z = (h0, c0)
                if not hasattr(self, 'z'):
                    self.z = z
            # Conv --> RNN
            hi = hi.transpose(1, 2)
            hi, state = self.rnn_core(hi, z)
            # RNN --> Conv
            hi = hi.transpose(1, 2)
        else:
            if not self.no_z:
                if z is None:
                    # make z 
                    z = Variable(torch.randn(hi.size(0), self.z_dim,
                                             *hi.size()[2:]))
                if len(z.size()) != len(hi.size()):
                    raise ValueError('len(z.size) {} != len(hi.size) {}'
                                     ''.format(len(z.size()), len(hi.size())))
                if self.do_cuda:
                    z = z.cuda()
                if not hasattr(self, 'z'):
                    self.z = z
                hi = torch.cat((hi, z), dim=1)
                if ret_hid:
                    hall['enc_zc'] = hi
            else:
                z = None
        #print('Concated hi|z size: ', hi.size())
        enc_layer_idx = len(self.gen_enc) - 1
        z_up = z
        for l_i, dec_layer in enumerate(self.gen_dec):
            if self.skip and enc_layer_idx in self.skips:
                skip_conn = skips[enc_layer_idx]
                hi = self.skip_merge(skip_conn, hi)
            if l_i > 0 and self.z_all:
                # concat z in every layer
                z_up = torch.cat((z_up, z_up), dim=2)
                hi = torch.cat((hi, z_up), dim=1)
            #print('DEC in size after skip and z_all: ', hi.size())
            hi = dec_layer(hi)
            enc_layer_idx -= 1
            if ret_hid:
                hall['dec_{}'.format(l_i)] = hi
        if hasattr(self, 'aal_out'):
            hi = self.aal_out(hi)
        if self.subtract_mean:
            hi = self.subtract_windowed_wav_mean(hi)
        # normalize G output in range within [-1, 1]
        #hi = self.batch_minmax_norm(hi)
        if ret_hid:
            return hi, hall
        else:
            return hi

    def batch_minmax_norm(self, x, out_min=-1, out_max=1):
        mins = torch.min(x, dim=2)[0]
        maxs = torch.max(x, dim=2)[0]
        R = (out_max - out_min) / (maxs - mins)
        R = R.unsqueeze(1)
        #print('R size: ', R.size())
        #print('x size: ', x.size())
        #print('mins size: ', mins.size())
        x = R * (x - mins.unsqueeze(1)) + out_min
        #print('norm x size: ', x.size())
        return x

    def subtract_windowed_wav_mean(self, wavb, W=20):
        cwavb = Variable(torch.zeros(wavb.size()))
        if self.do_cuda:
            cwavb = cwavb.cuda()
        for n in range(0, wavb.size(2), W):
            mn = torch.mean(wavb[:, :, n:n + W])
            cwavb[:, :, n:n + W] = wavb[:, :, n:n + W] - mn
        return cwavb

    def skip_merge(self, skip_conn, hi):
        hj = skip_conn['tensor']
        alpha = skip_conn['alpha'].view(1, -1, 1)
        alpha = alpha.repeat(hj.size(0), 1, hj.size(2))
        #print('hi: ', hi.size())
        #print('hj: ', hj.size())
        #print('alpha: ', alpha.size())
        #print('alpha: ', alpha)
        if 'dropout' in skip_conn:
            alpha = skip_conn['dropout'](alpha)
            #print('alpha: ', alpha)
        return hi + alpha * hj
        

class Generator(Model):

    def __init__(self, ninputs, enc_fmaps, kwidth, 
                 activations, bnorm=False, dropout=0.,
                 pooling=2, z_dim=1024, z_all=False,
                 skip=True, skip_blacklist=[],
                 dec_activations=None, cuda=False,
                 bias=False, aal=False, wd=0.,
                 core2d=False, core2d_kwidth=None, 
                 core2d_felayers=1,
                 skip_mode='concat'):
        # aal: anti-aliasing filter prior to each striding conv in enc
        super().__init__(name='Generator')
        self.skip_mode = skip_mode
        self.skip = skip
        self.z_dim = z_dim
        self.z_all = z_all
        self.do_cuda = cuda
        self.core2d = core2d
        self.wd = wd
        self.skip_blacklist = skip_blacklist
        if core2d_kwidth is None:
            core2d_kwidth = kwidth
        self.gen_enc = nn.ModuleList()
        if aal:
            # Make cheby1 filter to include into pytorch conv blocks
            from scipy.signal import cheby1, dlti, dimpulse
            system = dlti(*cheby1(8, 0.05, 0.8 / 2))
            tout, yout = dimpulse(system)
            filter_h = yout[0]
            self.filter_h = filter_h
        else:
            self.filter_h = None

        if isinstance(activations, str):
            activations = getattr(nn, activations)()
        if not isinstance(activations, list):
            activations = [activations] * len(enc_fmaps)
        # always begin with 1D block
        for layer_idx, (fmaps, act) in enumerate(zip(enc_fmaps, 
                                                     activations)):
            if layer_idx == 0:
                inp = ninputs
            else:
                inp = enc_fmaps[layer_idx - 1]
            if core2d:
                if layer_idx < core2d_felayers:
                    self.gen_enc.append(GBlock(inp, fmaps, kwidth, act,
                                               padding=None, bnorm=bnorm, 
                                               dropout=dropout, pooling=pooling,
                                               enc=True, bias=bias, 
                                               aal_h=self.filter_h))
                else:
                    if layer_idx == core2d_felayers:
                        # fmaps is 1 after conv1d blocks
                        inp = 1
                    self.gen_enc.append(G2Block(inp, fmaps, core2d_kwidth, act,
                                                padding=None, bnorm=bnorm, 
                                                dropout=dropout, pooling=pooling,
                                                enc=True, bias=bias))
            else:
                self.gen_enc.append(GBlock(inp, fmaps, kwidth, act,
                                           padding=None, bnorm=bnorm, 
                                           dropout=dropout, pooling=pooling,
                                           enc=True, bias=bias, 
                                           aal_h=self.filter_h))
        dec_inp = enc_fmaps[-1]
        if self.core2d:
            #dec_fmaps = enc_fmaps[::-1][1:-2]+ [1, 1]
            dec_fmaps = enc_fmaps[::-1][:-2] + [1, 1]
        else:
            dec_fmaps = enc_fmaps[::-1][1:]+ [1] 
        #print(dec_fmaps)
        #print(enc_fmaps)
        #print('dec_fmaps: ', dec_fmaps)
        self.gen_dec = nn.ModuleList()
        if dec_activations is None:
            dec_activations = activations
        
        dec_inp += z_dim

        for layer_idx, (fmaps, act) in enumerate(zip(dec_fmaps, 
                                                     dec_activations)):
            if skip and layer_idx > 0 and layer_idx not in skip_blacklist:
                #print('Adding skip conn input of idx: {} and size:'
                #      ' {}'.format(layer_idx, dec_inp))
                if self.skip_mode == 'concat':
                    dec_inp += enc_fmaps[-(layer_idx+1)]

            if z_all and layer_idx > 0:
                dec_inp += z_dim

            if layer_idx >= len(dec_fmaps) - 1:
                #act = None #nn.Tanh()
                act = nn.Tanh()
                bnorm = False
                dropout = 0

            if layer_idx < len(dec_fmaps) -1 and core2d:
                self.gen_dec.append(G2Block(dec_inp,
                                            fmaps, core2d_kwidth + 1, act, 
                                            padding=core2d_kwidth//2, 
                                            bnorm=bnorm,
                                            dropout=dropout, pooling=pooling, 
                                            enc=False,
                                            bias=bias))
            else:
                if layer_idx == len(dec_fmaps) - 1:
                    # after conv2d channel condensation, fmaps mirror the ones
                    # extracted in 1D encoder
                    dec_inp = enc_fmaps[0]
                    if skip and layer_idx not in skip_blacklist:
                        dec_inp += enc_fmaps[-(layer_idx+1)]
                self.gen_dec.append(GBlock(dec_inp,
                                           fmaps, kwidth + 1, act, 
                                           padding=kwidth//2, 
                                           bnorm=bnorm,
                                           dropout=dropout, pooling=pooling, 
                                           enc=False,
                                           bias=bias))
            dec_inp = fmaps

    def forward(self, x, z=None):
        hi = x
        skips = []
        for l_i, enc_layer in enumerate(self.gen_enc):
            hi = enc_layer(hi)
            #print('ENC {} hi size: {}'.format(l_i, hi.size()))
            if self.skip and l_i < (len(self.gen_enc) - 1):
                #print('Appending skip connection')
                skips.append(hi)
            #print('hi size: ', hi.size())
        #print('=' * 50)
        skips = skips[::-1]
        if z is None:
            # make z 
            #z = Variable(torch.randn(x.size(0), self.z_dim, hi.size(2)))
            #z = Variable(torch.randn(*hi.size()))
            z = Variable(torch.randn(hi.size(0), self.z_dim,
                                     *hi.size()[2:]))
        if len(z.size()) != len(hi.size()):
            raise ValueError('len(z.size) {} != len(hi.size) {}'
                             ''.format(len(z.size()), len(hi.size())))
        if self.do_cuda:
            z = z.cuda()
        if not hasattr(self, 'z'):
            self.z = z
        #print('z size: ', z.size())
        hi = torch.cat((hi, z), dim=1)
        #print('Input to dec after concating z and enc out: ', hi.size())
        #print('Enc out size: ', hi.size())
        z_up = z
        for l_i, dec_layer in enumerate(self.gen_dec):
            #print('dec layer: {} with input: {}'.format(l_i, hi.size()))
            #print('DEC in size: ', hi.size())
            if self.skip and l_i > 0 and l_i not in self.skip_blacklist:
                skip_conn = skips[l_i - 1]
                #print('concating skip {} to hi {}'.format(skip_conn.size(),
                #                                          hi.size()))
                hi = self.skip_merge(skip_conn, hi)
                #print('Merged hi: ', hi.size())
                #hi = torch.cat((hi, skip_conn), dim=1)
            if l_i > 0 and self.z_all:
                # concat z in every layer
                #print('z.size: ', z.size())
                z_up = torch.cat((z_up, z_up), dim=2)
                hi = torch.cat((hi, z_up), dim=1)
            hi = dec_layer(hi)
            #print('-' * 20)
            #print('hi size: ', hi.size())
        return hi

    def skip_merge(self, skip, hi):
        if self.skip_mode == 'concat':
            if len(hi.size()) == 4 and len(skip.size()) == 3:
                hi = hi.squeeze(1)
            # 1-D case
            hi_ = torch.cat((skip, hi), dim=1)
        elif self.skip_mode == 'sum':
            hi_ = skip + hi
        else:
            raise ValueError('Urecognized skip mode: ', self.skip_mode)
        return hi_


    def parameters(self):
        params = []
        for k, v in self.named_parameters():
            if 'aal_conv' not in k:
                params.append({'params':v, 'weight_decay':self.wd})
            else:
                print('Excluding param: {} from Genc block'.format(k))
        return params

if __name__ == '__main__':
    G = Generator1D(1, [64, 128, 256], 31, 'ReLU',
                    lnorm=True, dropout=0.5,
                    pooling=2,
                    z_dim=256,
                    z_all=True,
                    skip_init='randn',
                    skip_blacklist=[],
                    bias=True, cuda=False,
                    rnn_core=True, linterp=False,
                    dec_kwidth=2)
    print(G)
    x = Variable(torch.randn(1, 1, 16384))
    y = G(x)
    print(y)
    """
    G = Generator(1, [16, 32, 64, 64, 128, 256, 32, 32, 64, 64, 128, 128, 256, 256], 3, 'ReLU',
                  True, 0.5,
                  z_dim=256,
                  z_all=False,
                  skip_blacklist=[],
                  core2d=True,
                  core2d_felayers=6,
                  bias=True, cuda=True)
    G.parameters()
    G.cuda()
    print(G)
    x = Variable(torch.randn(1, 1, 16384)).cuda()
    y = G(x)
    print(y)
    """
