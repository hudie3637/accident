import os

import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import torch
from scipy.sparse.linalg import eigs
from torch_geometric.utils import dense_to_sparse, get_laplacian, to_dense_adj

from accident import Accident, AccidentGraph
from fusion_graph import FusionGraphModel


# Chebyshev Polynomial functions
def cheb_polynomial_torch(L_tilde, K):
    N = L_tilde.shape[0]
    cheb_polynomials = [torch.eye(N).to(L_tilde.device).float(), L_tilde.clone().float()]
    for i in range(2, K):
        cheb_polynomials.append((2 * L_tilde @ cheb_polynomials[i - 1] - cheb_polynomials[i - 2]).float())
    return cheb_polynomials

def cheb_polynomial(L_tilde, K):
    N = L_tilde.shape[0]
    cheb_polynomials = [np.identity(N), L_tilde.copy()]
    for i in range(2, K):
        cheb_polynomials.append(2 * L_tilde @ cheb_polynomials[i - 1] - cheb_polynomials[i - 2])
    return cheb_polynomials

def scaled_Laplacian(W):
    assert W.shape[0] == W.shape[1]
    D = np.diag(np.sum(W, axis=1))
    L = D - W
    lambda_max = eigs(L, k=1, which='LR')[0].real
    return (2 * L) / lambda_max - np.identity(W.shape[0])

# Chebyshev Convolution Layer
class cheb_conv(nn.Module):
    def __init__(self, K, fusiongraph, in_channels, out_channels, device):
        super(cheb_conv, self).__init__()
        self.K = K
        self.fusiongraph = fusiongraph
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.DEVICE = device
        self.Theta = nn.ParameterList(
            [nn.Parameter(torch.FloatTensor(in_channels, out_channels).to(self.DEVICE)) for _ in range(K)])
    def forward(self, x):
        '''
               Chebyshev graph convolution operation
               :param x: (batch_size, N, F_in, T)
               :return: (batch_size, N, F_out, T)
               '''

        x = x.float()

        #print(f'cheb_conv x{x.shape}')
        adj_for_run = self.fusiongraph()

        edge_idx, edge_attr = dense_to_sparse(adj_for_run)
        # edge_idx_l, edge_attr_l = get_laplacian(edge_idx, edge_attr, 'sym')
        edge_idx_l, edge_attr_l = get_laplacian(edge_idx, edge_attr)

        L_tilde = to_dense_adj(edge_idx_l, edge_attr=edge_attr_l)[0]
        cheb_polynomials = cheb_polynomial_torch(L_tilde, self.K)

        batch_size,  num_of_vertices,in_channels, num_of_timesteps = x.shape
       # print(f'in_channels{self.in_channels},out_channels{self.out_channels}')
     #   print(f'num_of_vertices{num_of_vertices}')

        outputs = []

        for time_step in range(num_of_timesteps):

            graph_signal = x[:, :, :, time_step]

            output = torch.zeros(batch_size, num_of_vertices, self.out_channels).to(self.DEVICE)  # (b, N, F_out)

            for k in range(self.K):
                T_k = cheb_polynomials[k]  # (N,N)

                theta_k = self.Theta[k]  # (in_channel, out_channel)
               # print(f'T_k{T_k.shape}')
               # print(f'graph_signal{graph_signal.shape}')
                #print(f'theta_k{theta_k.shape}')
                rhs = graph_signal.permute(0, 2, 1).matmul(T_k).permute(0, 2, 1)
               # print(f'rhs{rhs.shape}')
                h = rhs.matmul(theta_k)
              #  print(f'h{h.shape}')
               # print(f'output{output.shape}')

                output = output + h


            outputs.append(output.unsqueeze(-1))

        return F.relu(torch.cat(outputs, dim=-1))




# RNN Layer
class RNNLayer(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, num_layers):
        super(RNNLayer, self).__init__()
        self.rnn = nn.RNN(input_size, hidden_size, num_layers, batch_first=True)
        self.linear = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        x = x.float()
        rnn_out, _ = self.rnn(x)
        last_time_step = rnn_out[:, -1, :]
        return self.linear(last_time_step)

# GTAMN Block
class GTAMN_block(nn.Module):
    def __init__(self, in_channels, K, nb_chev_filter, nb_time_filter, time_strides, fusiongraph, device):
        super(GTAMN_block, self).__init__()
        self.cheb_conv = cheb_conv(K, fusiongraph, in_channels, nb_chev_filter, device)
        self.time_conv = nn.Conv2d(nb_chev_filter, nb_time_filter, kernel_size=(1, 3), stride=(1, time_strides), padding=(0, 1))
        self.residual_conv = nn.Conv2d(in_channels, nb_time_filter, kernel_size=(1, 1), stride=(1, time_strides))
        self.ln = nn.LayerNorm(nb_time_filter)

    def forward(self, x):
        '''
        :param x: (batch_size, N, F_in, T)
        :return: (batch_size, N, nb_time_filter, T)
        '''
        x = x.float()

        # cheb gcn
        spatial_gcn = self.cheb_conv(x)  # (b,N,F,T)
     #   print(f'spatial_gcn{spatial_gcn.shape}')

        # convolution along the time axis
        time_conv_output = self.time_conv(spatial_gcn.permute(0, 2, 1, 3))  # (b,F,N,T)
       # print(f'time_conv_output{time_conv_output.shape}')

        # residual shortcut
        x_residual = self.residual_conv(x.permute(0, 2, 1, 3))  # (b,F,N,T)
       # print(f'x_residual{x_residual.shape}')

        x_residual = self.ln(F.relu(x_residual + time_conv_output).permute(0, 3, 2, 1)).permute(0, 2, 3, 1)  # (b,N,F,T)

        return x_residual
# GTAMN Submodule
class GTAMN_submodule(nn.Module):
    def __init__(self, gpu_id, fusiongraph, in_channels, len_input, num_for_predict, hidden_size):
        super(GTAMN_submodule, self).__init__()
        device = 'cuda:%d' % gpu_id if torch.cuda.is_available() else 'cpu'
        self.device = device

        K = 3
        nb_block = 2
        nb_chev_filter = 24
        nb_time_filter = 24
        time_strides = 1

        self.BlockList = nn.ModuleList([
            GTAMN_block(in_channels, K, nb_chev_filter, nb_time_filter, time_strides, fusiongraph, device)
        ])

        self.BlockList.extend([
            GTAMN_block(nb_time_filter, K, nb_chev_filter, nb_time_filter, time_strides, fusiongraph, device)
            for _ in range(nb_block - 1)
        ])

        self.final_conv = nn.Conv2d(nb_time_filter, num_for_predict, kernel_size=(1, 1))
        # self.rnn = RNNLayer(nb_time_filter, hidden_size, hidden_size, num_rnn_layers)
        # self.regression_mlp = nn.Linear(hidden_size, num_for_predict)

        self.to(self.device)

    def forward(self, x):
        '''
        :param x: (B, N_nodes, F_in, T_in)
        :return: (B, N_nodes, T_out)
        '''
        x = x.permute(0, 2, 1, 3)
        x = x.requires_grad_()
        # 确保 x 需要梯度
        if not x.requires_grad:
            raise AssertionError("Input tensor x does not require gradient")
        i=0
        # 模型的其余前向传播代码
        for block in self.BlockList:
            i =  i + 1
            print(f'{i}block')
            x = block(x)
        x = x.requires_grad_()
        # 检查 x 是否仍然需要梯度
        if not x.requires_grad:
            raise AssertionError("Tensor x does not require gradient after passing through blocks")


        x = self.final_conv(x.permute(0, 2, 1, 3)).squeeze(dim=-1)
        prediction = x.permute(0, 2, 1)  # 调整维度以匹配 RNN 输入
        x = x.requires_grad_()
        # 再次检查 x 是否需要梯度
        if not x.requires_grad:
            raise AssertionError("Input tensor to RNN does not require gradient")

        # rnn_out= self.rnn(x)
        # rnn_out = rnn_out.view(rnn_out.size(0), -1)
        # prediction = self.regression_mlp(rnn_out)
        print(f'prediction:{prediction.shape}')
        return prediction