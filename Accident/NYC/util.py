from typing import Any, Callable, Optional
import torch
import numpy as np

from torch_geometric.utils import dense_to_sparse, get_laplacian, to_dense_adj
from torchmetrics import Metric


def get_L(W):
    edge_index, edge_weight = dense_to_sparse(W)
    edge_index, edge_weight = get_laplacian(edge_index, edge_weight)
    adj = to_dense_adj(edge_index, edge_attr=edge_weight)[0]
    return adj


def masked_mse(preds, labels, null_val=np.nan):
    if np.isnan(null_val):
        mask = ~torch.isnan(labels)
    else:
        mask = (labels!=null_val)
    mask = mask.float()
    mask /= torch.mean((mask))
    mask = torch.where(torch.isnan(mask), torch.zeros_like(mask), mask)
    loss = (preds-labels)**2
    loss = loss * mask
    loss = torch.where(torch.isnan(loss), torch.zeros_like(loss), loss)
    return torch.mean(loss)

def masked_rmse(preds, labels, null_val=np.nan):
    return torch.sqrt(masked_mse(preds=preds, labels=labels, null_val=null_val))


def masked_mae(preds, labels, null_val=np.nan):
    if np.isnan(null_val):
        mask = ~torch.isnan(labels)
    else:
        mask = (labels!=null_val)
    mask = mask.float()
    mask /= torch.mean((mask))
    mask = torch.where(torch.isnan(mask), torch.zeros_like(mask), mask)
    loss = torch.abs(preds-labels)
    loss = loss * mask
    loss = torch.where(torch.isnan(loss), torch.zeros_like(loss), loss)
    return torch.mean(loss)


def masked_mape(preds, labels, null_val=np.nan):
    # 过滤掉 null_val 值或者 NaN 值
    if np.isnan(null_val):
        mask = ~torch.isnan(labels)
    else:
        mask = (labels != null_val)

    # 过滤掉无效的标签值
    filtered_labels = labels[mask]  # 直接使用布尔索引
    filtered_preds = preds[mask]  # 添加这行代码以过滤预测值

    # 计算预测和标签之间的绝对误差
    error = torch.abs(filtered_preds - filtered_labels)

    # 避免除以零，这里假设 labels 中没有 0
    non_zero_mask = (filtered_labels > 0)
    error = error[non_zero_mask]
    filtered_labels = filtered_labels[non_zero_mask]

    # 计算 MAPE
    if torch.sum(non_zero_mask) == 0:  # 添加这行代码以避免除以零的错误
        return torch.tensor(0.0)  # 或者返回 None，或者抛出异常
    loss = error / filtered_labels

    # 计算平均 MAPE
    mean_loss = torch.mean(loss)

    return mean_loss

def metric(pred, real):
    # 确保 pred 和 real 的尺寸一致
    # print(f'pred {pred.shape},real {real.shape}')


    mae = masked_mae(pred.flatten(), real.flatten(), 0.0).item()
    mape = masked_mape(pred.flatten(), real.flatten(), 0.0).item()
    rmse = masked_rmse(pred.flatten(), real.flatten(), 0.0).item()
    return np.round(mae, 4), np.round(mape, 4), np.round(rmse, 4)

class LightningMetric(Metric):
    def __init__(self):
        super().__init__()

        self.add_state("y_true", default=[], dist_reduce_fx=None)
        self.add_state("y_pred", default=[], dist_reduce_fx=None)

    def update(self, preds: torch.Tensor, target: torch.Tensor):
        self.y_pred.append(preds)
        self.y_true.append(target)

    def compute(self):
        """
        Computes explained variance over state.
        """

        y_pred = torch.cat(self.y_pred, dim=0)
        y_true = torch.cat(self.y_true, dim=0)

        feature_dim = y_pred.shape[-1]
        pred_len = y_pred.shape[1]
        # (16, 12, 38, 1)

        y_pred = torch.reshape(y_pred.permute((0, 2, 1)), (-1, pred_len, feature_dim))
        y_true = torch.reshape(y_true.permute((0, 2, 1)), (-1, pred_len, feature_dim))

        # TODO: feature_dim, for multi-variable prediction, not only one.
        y_pred = y_pred[..., 0]
        y_true = y_true[..., 0]

        metric_dict = {}
        rmse_avg = []
        mae_avg = []
        mape_avg = []
        for i in range(pred_len):
            mae, mape, rmse = metric(y_pred[:, i], y_true[:, i])
            idx = i + 1

            # metric_dict.update({'rmse_%s' % idx: rmse})
            # metric_dict.update({'mae_%s' % idx: mae})
            # metric_dict.update({'mape_%s' % idx: mape})

            rmse_avg.append(rmse)
            mae_avg.append(mae)
            mape_avg.append(mape)

        metric_dict.update({'rmse_avg': np.round(np.mean(rmse_avg), 4)})
        metric_dict.update({'mae_avg': np.round(np.mean(mae_avg), 4)})
        metric_dict.update({'mape_avg': np.round(np.mean(mape_avg), 4)})

        return metric_dict

class StandardScaler():
    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def transform(self, data):

            return (data - self.mean) / self.std


    def inverse_transform(self, data):
        return (data * self.std) + self.mean


def cheb_polynomial(L_tilde, K):
    '''
    compute a list of chebyshev polynomials from T_0 to T_{K-1}

    Parameters
    ----------
    L_tilde: scaled Laplacian, np.ndarray, shape (N, N)

    K: the maximum order of chebyshev polynomials

    Returns
    ----------
    cheb_polynomials: list(np.ndarray), length: K, from T_0 to T_{K-1}

    '''

    N = L_tilde.shape[0]

    cheb_polynomials = [np.identity(N), L_tilde.copy()]

    for i in range(2, K):
        cheb_polynomials.append(2 * L_tilde * cheb_polynomials[i - 1] - cheb_polynomials[i - 2])

    return cheb_polynomials


if __name__ == '__main__':

    lightning_metric = LightningMetric()
    batches = 10
    for i in range(batches):
        preds = torch.randn(32, 24, 38, 1)
        target = preds + 0.15

        rmse_batch = lightning_metric(preds, target)
        print(f"Metrics on batch {i}: {rmse_batch}")

    rmse_epoch = lightning_metric.compute()
    print(f"Metrics on all data: {rmse_epoch}")

    lightning_metric.reset()