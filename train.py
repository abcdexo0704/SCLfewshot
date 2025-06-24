import argparse
import os
import torch
import torch.nn.functional as F
import datetime

from torch.utils.data import DataLoader
from datasets.samplers import CategoriesSampler
from datasets.miniimagenet import SSLMiniImageNet, MiniImageNet
from datasets.tiered_imagenet import SSLTieredImageNet, TieredImageNet
from datasets.cifarfs import SSLCifarFS, CIFAR_FS
from datasets.fc100 import SSLFC100, FC100
from resnet import resnet12
from util import str2bool, set_gpu, ensure_path, save_checkpoint, count_acc, seed_torch, Averager, compute_confidence_interval, normalize, Timer
from sklearn import metrics
from sklearn.linear_model import LogisticRegression
import random
from itertools import permutations
import torch.backends.cudnn as cudnn

def get_dataset(args):
    if args.dataset == 'mini':
        trainset = SSLMiniImageNet('train', args)
        valset = MiniImageNet('val', args.size)
        n_cls = 64
        print("=> MiniImageNet...")
    elif args.dataset == 'tiered':
        trainset = SSLTieredImageNet('train', args)
        valset = TieredImageNet('val', args.size)
        n_cls = 351
        print("=> TieredImageNet...")
    elif args.dataset == 'cifarfs':
        trainset = SSLCifarFS('train', args)
        valset = CIFAR_FS('val', args.size)
        n_cls = 64
        print("=> CIFAR-FS...")
    elif args.dataset == 'fc100':
        trainset = SSLFC100('train', args)
        valset = FC100('val', args.size)
        n_cls = 60
        print("=> FC100...")
    else:
        print("Invalid dataset...")
        exit()
    train_loader = DataLoader(dataset=trainset, batch_size=args.batch_size,
                                shuffle=True, drop_last=True,
                                num_workers=args.worker, pin_memory=True)

    val_sampler = CategoriesSampler(valset.label, args.test_batch,
                                    args.way, args.shot + args.query)
    val_loader = DataLoader(dataset=valset, batch_sampler=val_sampler,
                            num_workers=args.worker, pin_memory=True)
    return train_loader, val_loader, n_cls
def create_jigsaw_puzzle(image, grid_size=3):
    """創建拼圖任務"""
    batch_size, channels, height, width = image.shape
    patch_size_h = height // grid_size
    patch_size_w = width // grid_size
    
    # 分割圖像為patches
    patches = []
    for i in range(grid_size):
        for j in range(grid_size):
            patch = image[:, :, i*patch_size_h:(i+1)*patch_size_h, 
                         j*patch_size_w:(j+1)*patch_size_w]
            patches.append(patch)
    
    # 預定義一些排列組合（減少計算複雜度）
    predefined_perms = list(permutations(range(grid_size*grid_size)))[:100]
    
    shuffled_images = []
    perm_labels = []
    
    for b in range(batch_size):
        # 隨機選擇一個排列
        perm_idx = random.randint(0, len(predefined_perms)-1)
        perm = predefined_perms[perm_idx]
        
        # 重新排列patches
        shuffled_patches = [patches[perm[i]][b:b+1] for i in range(len(perm))]
        
        # 重組圖像
        rows = []
        for i in range(grid_size):
            row_patches = shuffled_patches[i*grid_size:(i+1)*grid_size]
            row = torch.cat(row_patches, dim=3)
            rows.append(row)
        shuffled_image = torch.cat(rows, dim=2)
        shuffled_images.append(shuffled_image)
        perm_labels.append(perm_idx)
    
    return torch.cat(shuffled_images, dim=0), torch.tensor(perm_labels)

def create_masked_images(image, mask_ratio=0.15):
    """創建遮擋任務"""
    batch_size, channels, height, width = image.shape
    masked_images = image.clone()
    
    for b in range(batch_size):
        # 隨機選擇要遮擋的位置
        num_masked = int(height * width * mask_ratio)
        flat_indices = torch.randperm(height * width)[:num_masked]
        
        for idx in flat_indices:
            h_idx = idx // width
            w_idx = idx % width
            masked_images[b, :, h_idx, w_idx] = 0  # 用0遮擋
    
    return masked_images
def main(args):
    if args.detail:
        print("=> Training begin...")
        print("--------------------------------------------------------")
    ensure_path(args.save_path)

    train_loader, val_loader, n_cls = get_dataset(args)
    # model
    if args.dataset in ['mini', 'tiered']:
        model = resnet12(avg_pool=True, drop_rate=0.1, dropblock_size=5, num_classes=n_cls).cuda()
    elif args.dataset in ['cifarfs', 'fc100']:
        model = resnet12(avg_pool=True, drop_rate=0.1, dropblock_size=2, num_classes=n_cls).cuda()
    else:
        print("Invalid dataset...")
        exit()

    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.wd)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, args.lr_decay_epochs, gamma=args.lr_decay_rate)
    # cuda setting
    cudnn.benchmark = True
    
    trlog = {}
    trlog['args'] = vars(args)
    trlog['train_loss'] = []
    trlog['train_acc'] = []
    trlog['val_loss'] = []
    trlog['val_acc'] = []
    trlog['max_acc'] = 0.0
    trlog['best_epoch'] = 0
    start_epoch = 1
    cmi = [0.0, 0.0]
    timer = Timer()

    # check resume point
    checkpoint_file = os.path.join(args.save_path, 'checkpoint.pth.tar')
    if os.path.isfile(checkpoint_file):
        checkpoint = torch.load(checkpoint_file)
        trlog = checkpoint['trlog']
        start_epoch = checkpoint['start_epoch'] + 1
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        cmi[0] = trlog['max_acc']
        print("=> Resume from epoch {} ...".format(start_epoch))

    for epoch in range(start_epoch, args.epochs + 1):

        tl, ta = train(args, train_loader, model, optimizer)
        va, vb = validation(args, val_loader, model)
        lr_scheduler.step()

        if va > trlog['max_acc']:
            trlog['max_acc'] = va
            trlog['best_epoch'] = epoch
            cmi[0] = va
            cmi[1] = vb
            # save best model
            save_checkpoint({
                'best_epoch': epoch,
                'model': model.state_dict()
            }, args.save_path, name='max-acc')

        trlog['train_loss'].append(tl)
        trlog['train_acc'].append(ta)
        trlog['val_acc'].append(va)

        # checkpoint saving
        save_checkpoint({
            'start_epoch': epoch,
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'lr_scheduler': lr_scheduler.state_dict(),
            'trlog': trlog
        }, args.save_path)

        ot, ots = timer.measure()
        tt, _ = timer.measure(epoch / args.epochs)

        if args.detail:
            print('Epoch {}/{}: train loss {:.4f} - acc {:.2f}% - val acc {:.2f}% - best acc {:.2f}% - ETA:{}/{}'.format(
                epoch, args.epochs, tl, ta*100, va*100, trlog['max_acc']*100, ots, timer.tts(tt-ot)))

        if epoch >= args.epochs:
            print("Best Epoch is {} with acc={:.2f}±{:.2f}%...".format(trlog['best_epoch'], cmi[0]*100, cmi[1]*100))
            print("--------------------------------------------------------")
    #
    return

def dist_loss(data, batch_size):
    d_90 = data[batch_size:2*batch_size] - data[:batch_size]
    loss_a = torch.mean(torch.sqrt(torch.sum((d_90)**2, dim=1)))
    d_180 = data[2*batch_size:3*batch_size] - data[:batch_size]
    loss_a += torch.mean(torch.sqrt(torch.sum((d_180)**2, dim=1)))
    d_270 = data[3*batch_size:4*batch_size] - data[:batch_size]
    loss_a += torch.mean(torch.sqrt(torch.sum((d_270)**2, dim=1)))

    return loss_a

def preprocess_data(data , task_type='all'):
    processed_data = {}
    
    for idxx, img in enumerate(data):
        # 原始圖像和旋轉變體
        x = img.data[0].unsqueeze(0)
        x90 = img.data[1].unsqueeze(0).transpose(2,3).flip(2)
        x180 = img.data[2].unsqueeze(0).flip(2).flip(3)
        x270 = img.data[3].unsqueeze(0).flip(2).transpose(2,3)
        
        if idxx <= 0:
            xlist = x
            x90list = x90
            x180list = x180
            x270list = x270
        else:
            xlist = torch.cat((xlist, x), 0)
            x90list = torch.cat((x90list, x90), 0)
            x180list = torch.cat((x180list, x180), 0)
            x270list = torch.cat((x270list, x270), 0)
    
    # 旋轉任務數據
    processed_data['rotation'] = torch.cat((xlist, x90list, x180list, x270list), 0).cuda()
    
    if task_type in ['all', 'jigsaw']:
        # 拼圖任務數據
        jigsaw_images, jigsaw_labels = create_jigsaw_puzzle(xlist)
        processed_data['jigsaw'] = jigsaw_images.cuda()
        processed_data['jigsaw_labels'] = jigsaw_labels.cuda()
    
    if task_type in ['all', 'masked']:
        # 遮擋重建任務數據
        masked_images = create_masked_images(xlist)
        processed_data['masked'] = masked_images.cuda()
        processed_data['original'] = xlist.cuda()
    
    return processed_data

def train(args, dataloader, model, optimizer):
    model.train()

    tl = Averager()
    ta = Averager()

    for i, (inputs, target) in enumerate(dataloader, 1):
        target = target.cuda()
        
        # 多任務數據預處理
        processed_data = preprocess_data(inputs['data'], 'all')
        target = target.repeat(4)  # 為旋轉任務重複標籤

        # 旋轉標籤
        rot_labels = torch.zeros(4*args.batch_size).cuda().long()
        for j in range(4*args.batch_size):  # 改為 j
            if j < args.batch_size:
                rot_labels[j] = 0
            elif j < 2*args.batch_size:
                rot_labels[j] = 1
            elif j < 3*args.batch_size:
                rot_labels[j] = 2
            else:
                rot_labels[j] = 3

        # 計算各任務損失
        total_loss = 0
        
        # 1. 旋轉任務
        _, train_logit, rot_logits = model(processed_data['rotation'], ssl=True)
        rot_labels_onehot = F.one_hot(rot_labels.to(torch.int64), 4).float()
        loss_rot = torch.sum(F.binary_cross_entropy_with_logits(
            input=rot_logits, target=rot_labels_onehot))
        loss_rot = args.gamma_rot * loss_rot
        
        # 2. 對比學習（距離損失）
        loss_dist = dist_loss(train_logit, args.batch_size)
        if(torch.isnan(loss_dist).any()):
            print("Skip this loop")
            break
        loss_dist = args.gamma_dist * (loss_dist / 3.0)
        
        # 3. 基礎分類損失
        loss_ce = F.cross_entropy(train_logit, target)
        
        # 4. 拼圖任務
        if hasattr(args, 'gamma_jigsaw') and args.gamma_jigsaw > 0:
            _, _, jigsaw_logits = model(processed_data['jigsaw'], jigsaw=True)
            loss_jigsaw = F.cross_entropy(jigsaw_logits, processed_data['jigsaw_labels'])
            loss_jigsaw = args.gamma_jigsaw * loss_jigsaw
            total_loss += loss_jigsaw
        
        # 5. 遮擋重建任務
        if hasattr(args, 'gamma_masked') and args.gamma_masked > 0:
            original_feat, _, reconstructed_feat = model(processed_data['masked'], masked=True)
            target_feat = model(processed_data['original'], is_feat=True)
            loss_masked = F.mse_loss(reconstructed_feat, target_feat.detach())
            loss_masked = args.gamma_masked * loss_masked
            total_loss += loss_masked
        
        # 總損失
        loss = loss_ce + loss_rot + loss_dist + total_loss
        acc = count_acc(train_logit, target)

        # 結果記錄
        tl.add(loss.item())
        ta.add(acc)

        # 反向傳播
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    return tl.item(), ta.item()

def validation(args, dataloader, model):
    model.eval()
    acc_list = []

    for _, batch in enumerate(dataloader, 1):
        data, _ = [_.cuda() for _ in batch]
        p = args.shot * args.way
        data_shot, data_query = data[:p], data[p:]

        sf = model(data_shot, is_feat=args.is_feat)
        qf = model(data_query, is_feat=args.is_feat)

        py = torch.arange(args.way).repeat(args.shot)
        py = py.type(torch.LongTensor)
        qy = torch.arange(args.way).repeat(args.query)
        qy = qy.type(torch.LongTensor)

        if args.norm:
            sf = normalize(sf)
            qf = normalize(qf)

        sf = sf.detach().cpu().numpy()
        qf = qf.detach().cpu().numpy()
        py = py.view(-1).numpy()
        qy = qy.view(-1).numpy()
        # LR
        clf = LogisticRegression(penalty='l2',
                                    random_state=0,
                                    C=1.0,
                                    solver='lbfgs',
                                    max_iter=1000,
                                    multi_class='multinomial')
        clf.fit(sf, py)
        query_ys_pred_logit = clf.predict(qf)
        acc_list.append(metrics.accuracy_score(qy, query_ys_pred_logit))

    a, b = compute_confidence_interval(acc_list)
    return a, b


if __name__ == '__main__':
    start_time = datetime.datetime.now()
    # settings
    parser = argparse.ArgumentParser()
    parser.add_argument('--save-path', default='./save/exp1')
    parser.add_argument('--gpu', default='0')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--detail', type=str2bool, nargs='?', default=True)
    # network
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--lr', type=float, default=0.05)
    parser.add_argument('--wd', type=float, default=5e-4)
    parser.add_argument('--lr-decay-epochs', type=str, default='60,80')
    parser.add_argument('--lr-decay-rate', type=float, default=0.1)
    parser.add_argument('--momentum', type=float, default=0.9)
    # ssl
    parser.add_argument('--gamma-rot', type=float, default=2.5)
    parser.add_argument('--gamma-dist', type=float, default=0.02)
    # 新增任務參數
    parser.add_argument('--gamma-jigsaw', type=float, default=0.02)
    parser.add_argument('--gamma-masked', type=float, default=0.02)
    # dataset
    parser.add_argument('--dataset', default='mini', choices=['mini','tiered','cifarfs','fc100'])
    parser.add_argument('--size', type=int, default=84)
    parser.add_argument('--worker', type=int, default=8)
    # few-shot
    parser.add_argument('--way', type=int, default=5)
    parser.add_argument('--shot', type=int, default=1)
    parser.add_argument('--query', type=int, default=15)
    parser.add_argument('--test-batch', type=int, default=2000)
    parser.add_argument('--norm', type=str2bool, nargs='?', default=True)
    parser.add_argument('--is-feat', type=str2bool, nargs='?', default=True)
    args = parser.parse_args()
    #
    iterations = args.lr_decay_epochs.split(',')
    args.lr_decay_epochs = list([])
    for it in iterations:
        args.lr_decay_epochs.append(int(it))
    
    if args.dataset in ['mini', 'tiered']:
        args.size = 84
    elif args.dataset in ['cifarfs','fc100']:
        args.size = 32
        args.worker = 0
    # fix random seed
    seed_torch(args.seed)
    set_gpu(args.gpu)

    main(args)

    end_time = datetime.datetime.now()
    print("End time :{} total ({})".format(end_time, end_time - start_time))

