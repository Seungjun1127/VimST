import sys
sys.path.insert(0, '/root/Mamba-ITS/mamba_its/code')
import os

import argparse
from random import seed
import pandas as pd
import numpy as np
from tqdm import tqdm
from typing import Dict, List, Optional, Set, Tuple, Union
import time
import psutil
import torch
from transformers import EarlyStoppingCallback, IntervalStrategy
from transformers import AutoConfig, AutoFeatureExtractor, AutoModelForImageClassification, TrainingArguments, Trainer
from sklearn.metrics import *
from torchvision.transforms import (
    Compose,
    Normalize,
    ToTensor,
    Resize,
)

from evaluate import load as load_metric

from datasets import load_dataset
from datasets import Dataset, Image

from Vision.load_data import get_data_split 
from models.vit.modeling_vit import ViTForImageClassification
from models.swin.modeling_swin import SwinForImageClassification
# from models.mamba.modeling_mamba import MambaForImageClassification
from datetime import datetime

def one_hot(y_):
    y_ = y_.reshape(len(y_))

    y_ = [int(x) for x in y_]
    n_values = np.max(y_) + 1
    return np.eye(n_values)[np.array(y_, dtype=np.int32)]


def fine_tune_hf(
    model_path,
    model_loader,
    output_dir,
    dataset,
    train_dataset,
    val_dataset,
    test_dataset,
    image_size,
    grid_layout,
    window_size,
    num_classes,
    epochs,
    train_batch_size,
    eval_batch_size,
    save_steps,
    logging_steps,
    learning_rate,
    seed,
    save_total_limit,
    load_best_model_at_end,
    do_train,
    continue_training,
    train_from_scratch
    ):  
        
    # loading model and feature extractor
    if do_train and not continue_training:
        if train_from_scratch:
            config = AutoConfig.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True)
            # update config
            config.image_size = image_size
            config.grid_layout = grid_layout
            if window_size and "window_size" in config:
                config.window_size = window_size 
            model = model_loader(config)
        else:
            if "mamba" in model_path:
                model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, trust_remote_code=True)
            elif "vit" in model_path:
                model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, 
                                                        image_size=image_size, 
                                                        grid_layout=grid_layout)
            elif "swin" in model_path:
                model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, 
                                                        image_size=image_size, 
                                                        grid_layout=grid_layout)
            else:
                model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, trust_remote_code=True)
        #feature_extractor = AutoFeatureExtractor.from_pretrained(model_path)
    else:
        # if not train, load the fine-tuned model saved in output_dir
        if os.path.exists(output_dir):
            dir_list = os.listdir(output_dir) # find the latest checkpoint
            latest_checkpoint_idx = 0
            for d in dir_list:
                if "checkpoint" in d:
                    checkpoint_idx = int(d.split("-")[-1])
                    if checkpoint_idx > latest_checkpoint_idx:
                        latest_checkpoint_idx = checkpoint_idx

            if latest_checkpoint_idx > 0 and os.path.exists(os.path.join(output_dir, f"checkpoint-{latest_checkpoint_idx}")):
                ft_model_path = os.path.join(output_dir, f"checkpoint-{latest_checkpoint_idx}")
                # feature_extractor = AutoFeatureExtractor.from_pretrained(ft_model_path)
                model = model_loader.from_pretrained(ft_model_path, num_labels=num_classes, ignore_mismatched_sizes=True, trust_remote_code=True )
            else: # don't have a fine-tuned model
                #feature_extractor = AutoFeatureExtractor.from_pretrained(model_path)
                if train_from_scratch:
                    config = AutoConfig.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, image_size=image_size, grid_layout=grid_layout)
                    model = model_loader.from_config(config)
                else:
                    model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, trust_remote_code=True)
                    # model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, image_size=image_size, grid_layout=grid_layout)
        else:
            #feature_extractor = AutoFeatureExtractor.from_pretrained(model_path)
            if train_from_scratch:
                config = AutoConfig.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, image_size=image_size, grid_layout=grid_layout)
                model = model_loader.from_config(config)
            else:
                model = model_loader.from_pretrained(model_path, num_labels=num_classes, ignore_mismatched_sizes=True, grid_layout=grid_layout)

    # define evaluation metric
    def compute_metrics_binary(eval_pred):
        """Computes accuracy on a batch of predictions"""
        predictions, labels = eval_pred

        metric = load_metric("accuracy")
        accuracy = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels)["accuracy"]
        metric = load_metric("precision")
        precision = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels)["precision"]
        metric = load_metric("recall")
        recall = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels)["recall"]
        metric = load_metric("f1")
        f1 = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels)["f1"]

        denoms = np.sum(np.exp(predictions), axis=1).reshape((-1, 1))
        probs = np.exp(predictions) / denoms

        auc = roc_auc_score(labels, probs[:, 1])
        aupr = average_precision_score(labels, probs[:, 1])

        return {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1, "auroc": auc, "auprc": aupr}
    
    def compute_metrics_multilabel(eval_pred):
        """Computes accuracy on a batch of predictions"""
        predictions, labels = eval_pred

        metric = load_metric("accuracy")
        accuracy = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels)["accuracy"]
        metric = load_metric("precision")
        precision = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels, average="macro")["precision"]
        metric = load_metric("recall")
        recall = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels, average="macro")["recall"]
        metric = load_metric("f1")
        f1 = metric.compute(predictions=np.argmax(predictions, axis=1), references=labels, average="macro")["f1"]

        denoms = np.sum(np.exp(predictions), axis=1).reshape((-1, 1))
        probs = np.exp(predictions) / denoms

        auc = 0
        aupr = 0

        return {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1, "auroc": auc, "auprc": aupr}
    
    def compute_metrics_regression(eval_pred):
        """Computes accuracy on a batch of predictions"""
        predictions, labels = eval_pred

        rmse = mean_squared_error(labels, predictions, squared=False)
        mape = mean_absolute_percentage_error(labels, predictions)
        mae = mean_absolute_error(labels, predictions)

        return {"rmse": rmse, "mape": mape, "mae": mae}

    # define image transformation function
#    normalize = Normalize(mean=feature_extractor.image_mean, std=feature_extractor.image_std)
    train_transforms = Compose(
            [   
                # Resize(feature_extractor.size),
                # RandomResizedCrop(feature_extractor.size),
                # CenterCrop(feature_extractor.size),
                Resize((224, 224)),
                ToTensor(),
                # normalize,
            ]
        )
    val_transforms = Compose(
            [
                # Resize(feature_extractor.size),
                # CenterCrop(feature_extractor.size),
                Resize((224, 224)),
                ToTensor(),
                # normalize,
            ]
        )

    def preprocess_train(example_batch):
        """Apply train_transforms across a batch."""
        example_batch["pixel_values"] = [
            train_transforms(image.convert("RGB")) for image in example_batch["image"]
        ]
        return example_batch

    def preprocess_val(example_batch):
        """Apply val_transforms across a batch."""
        example_batch["pixel_values"] = [val_transforms(image.convert("RGB")) for image in example_batch["image"]]
        return example_batch

    def collate_fn(examples):
        pixel_values = torch.stack([example["pixel_values"] for example in examples])
        #labels = torch.tensor([example["label"] for example in examples])
        labels = torch.tensor([example["label"][0] for example in examples])
        #print(labels)
        # return {"pixel_values": pixel_values, "labels": labels}
        return {"tensor": pixel_values, "labels": labels}

    # transform the dataset
    train_dataset.set_transform(preprocess_train)
    val_dataset.set_transform(preprocess_val)
    test_dataset.set_transform(preprocess_val)

    if num_classes == 1:
        compute_metrics = compute_metrics_regression
        best_metric = "rmse"
    elif num_classes == 2:
        compute_metrics = compute_metrics_binary
        if dataset in ['P19', 'P12']:
            best_metric = "auroc"
        else:
            best_metric = 'accuracy'
    elif num_classes > 2:
        compute_metrics = compute_metrics_multilabel
        best_metric = "accuracy"

#    print(model)
    # training arguments
    training_args = TrainingArguments(
    output_dir=output_dir,          # output directory
    num_train_epochs=epochs,              # total number of training epochs
    per_device_train_batch_size=train_batch_size,  # batch size per device during training
    per_device_eval_batch_size=eval_batch_size,   # batch size for evaluation
    evaluation_strategy = "steps",
    save_strategy = "steps",
    learning_rate=learning_rate, # 2e-5
    gradient_accumulation_steps=4,
    warmup_ratio=0.1,
    # fp16=True,
    # fp16_backend="amp",
    save_steps=save_steps,
    logging_steps=logging_steps,
    logging_dir=os.path.join(output_dir, "runs/"),
    save_total_limit=save_total_limit,
    seed=seed,
    load_best_model_at_end=load_best_model_at_end,
    remove_unused_columns=False,
    metric_for_best_model=best_metric
    )

    trainer = Trainer(
    model,
    training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    #tokenizer=feature_extractor,
    compute_metrics=compute_metrics,
    data_collator=collate_fn,
    #callbacks=[EarlyStoppingCallback(early_stopping_patience=5)] if load_best_model_at_end else None
    callbacks= None
    )
    # training the model with Huggingface 🤗 trainer
    if do_train:
        start = time.time()
        train_results = trainer.train()
        end = time.time()
        time_elapsed = end - start
        print('Total Time elapsed: %.3f mins' % (time_elapsed / 60.0))
        
        # trainer.save_model()
        # trainer.log_metrics("train", train_results.metrics)
        # trainer.save_metrics("train", train_results.metrics)
        # trainer.save_state()
    
    # evaluation results
    # metrics = trainer.evaluate()
    # acc = metrics["eval_accuracy"]
    # precision = metrics["eval_precision"]
    # recall = metrics["eval_recall"]
    # F1 = metrics["eval_f1"]
    # auroc = metrics["eval_auroc"]
    # auprc = metrics["eval_auprc"]

    # testing results
    start = time.time()
    predictions = trainer.predict(test_dataset)
    end = time.time()
    time_elapsed = end - start
    print('Total Time elapsed: %.3f secs' % (time_elapsed))
    
    # Calculate throughput (samples per second)
    num_samples = len(test_dataset)
    throughput = num_samples / time_elapsed
    
    # Calculate GPU memory usage
    if torch.cuda.is_available():
        gpu_memory_usage_mb = torch.cuda.max_memory_allocated() / 1024 / 1024  # Convert to MB
        torch.cuda.empty_cache()
    else:
        gpu_memory_usage_mb = 0.0
    
    logits, labels = predictions.predictions, predictions.label_ids
    ypred = np.argmax(logits, axis=1)
    denoms = np.sum(np.exp(logits), axis=1).reshape((-1, 1))
    probs = np.exp(logits) / denoms

    if num_classes == 1:
        acc = precision = recall = F1 = auc = aupr = 0.
        rmse = mean_squared_error(labels, logits, squared=False)
        mape = mean_absolute_percentage_error(labels, logits)
        mae = mean_absolute_error(labels, logits)

    elif num_classes == 2:
        acc = np.sum(labels.ravel() == ypred.ravel()) / labels.shape[0]
        precision = precision_score(labels, ypred)
        recall = recall_score(labels, ypred)
        F1 = f1_score(labels, ypred)
        auc = roc_auc_score(labels, probs[:, 1])
        aupr = average_precision_score(labels, probs[:, 1])
        rmse = mape = mae = 0.

    elif num_classes > 2:
        acc = np.sum(labels.ravel() == ypred.ravel()) / labels.shape[0]
        precision = precision_score(labels, ypred, average="macro")
        recall = recall_score(labels, ypred, average="macro") 
        auc = 0
        aupr = 0
        F1 = f1_score(labels, ypred, average="macro")
        rmse = mape = mae = 0.

    return acc, precision, recall, F1, auc, aupr, rmse, mape, mae, throughput, gpu_memory_usage_mb


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    
    # arguments for dataset
    parser.add_argument('--dataset', type=str, default='P12') #
    parser.add_argument('--dataset_prefix', type=str, default='') #
    
    parser.add_argument('--withmissingratio', default=False, help='if True, missing ratio ranges from 0 to 0.5; if False, missing ratio =0') #
    parser.add_argument('--feature_removal_level', type=str, default='no_removal', choices=['no_removal', 'set', 'random'],
                        help='use this only when splittype==random; otherwise, set as no_removal') #
    parser.add_argument('--predictive_label', type=str, default='mortality', choices=['mortality', 'LoS'],
                        help='use this only with P12 dataset (mortality or length of stay)')
    
    # arguments for huggingface training
    parser.add_argument('--model', type=str, default='vit') #
    parser.add_argument('--model_path', type=str, default=None)
    parser.add_argument('--output_dir', type=str, default=None)
    parser.add_argument('--seed', type=int, default=1799)
    parser.add_argument('--save_total_limit', type=int, default=1)
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--train_batch_size', type=int, default=32)
    parser.add_argument('--eval_batch_size', type=int, default=64)
    parser.add_argument('--logging_steps', type=int, default=5)
    parser.add_argument('--save_steps', type=int, default=5)

    parser.add_argument('--learning_rate', type=float, default=2e-5)
    parser.add_argument('--n_runs', type=int, default=1)
    parser.add_argument('--n_splits', type=int, default=5)
    parser.add_argument('--upsample', default=False)

    # argument for the images
    parser.add_argument('--grid_layout', default=None)
    parser.add_argument('--image_size', default=None)
    parser.add_argument('--patch_size', default=None)
    parser.add_argument('--window_size', default=None)
    parser.add_argument('--mask_patch_size', type=int, default=None)
    parser.add_argument('--mask_ratio', type=float, default=None)
    parser.add_argument('--mask_method', type=str, default=None)

    # argument for ablation study
    parser.add_argument('--do_train', action='store_true')
    parser.add_argument('--continue_training', action='store_true', help='whether to load a fine-tuned model to continue')
    parser.add_argument('--train_from_scratch', action='store_true', help='whether to load a randomly initialized model')
    parser.add_argument('--finetune_mim', action='store_true')
    parser.add_argument('--finetune_mae', action='store_true')
    
    # memory optimization arguments
    parser.add_argument('--gradient_checkpointing', action='store_true', help='enable gradient checkpointing to save memory')
    parser.add_argument('--fp16', action='store_true', help='enable mixed precision training')
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1, help='number of steps to accumulate gradients')
    parser.add_argument('--dataloader_num_workers', type=int, default=0, help='number of dataloader workers')
    parser.add_argument('--dataloader_pin_memory', action='store_true', help='pin memory in dataloader')

    args = parser.parse_args()

    dataset = args.dataset
    dataset_prefix = args.dataset_prefix
    print(f'Dataset used: {dataset}, prefix: {dataset_prefix}.')

    epochs = args.epochs
    upsample = args.upsample
    image_size = args.image_size
    grid_layout = args.grid_layout
    window_size = args.window_size
    mask_patch_size = args.mask_patch_size
    mask_ratio = args.mask_ratio
    mask_method = args.mask_method
    patch_size = args.patch_size
    if dataset == 'P12':
        base_path = '../../dataset/P12data'
        num_classes = 2
        upsample = True
        epochs = 4
        image_size = 384
        grid_layout = 6
    elif dataset == 'P19':
        base_path = '../../dataset/P19data'
        num_classes = 2
        image_size = 384
        grid_layout = 6
        upsample = True
        epochs = 2
    elif dataset == 'PAM':
        base_path = '../../dataset/PAMdata'
        num_classes = 8
        grid_layout = (4, 5)
        image_size = (256, 320)
        epochs = 20
    """prepare the model for sequence classification"""
    model = args.model
    model_loader = AutoModelForImageClassification
    if model == "mambavisionB21K": 
        model_path = "nvidia/MambaVision-B-21K" # TBD
        pretrained_data = "ImageNet-21k"
        pretrained_size = 224
    if model == "nvidia/MambaVision-T-1K": 
        model_path = "nvidia/MambaVision-T-1K" # TBD
        model_loader = MambaForImageClassification
        pretrained_data = "ImageNet-1k"
        pretrained_size = 224
    if model == "mamba": # default mamba
        model_path = "pass" # TBD
        patch_size = 16 # TBD
        pretrained_data = "ImageNet-21k"
        pretrained_size = 224
    if model == "vit": # default vit
        model_path = "google/vit-base-patch16-224-in21k"
        model_loader = ViTForImageClassification
        patch_size = 16
        pretrained_data = "ImageNet-21k"
        pretrained_size = 224
    elif model == "swin": # default swin
        model_path = "microsoft/swin-base-patch4-window7-224-in22k"
        model_loader = SwinForImageClassification
        patch_size = 16
        pretrained_data = "ImageNet-21k"
        pretrained_size = 224
    elif model == "resnet":
        model_path = "microsoft/resnet-50"
        pretrained_data = "ImageNet-1k"
        pretrained_size = 224
        
    feature_removal_level = args.feature_removal_level  # 'set' for fixed, 'sample' for random sample
    print(feature_removal_level)

    """While missing_ratio >0, feature_removal_level is automatically used"""
    if bool(args.withmissingratio) == True:
        missing_ratios = [0.1, 0.2, 0.3, 0.4, 0.5]
    else:
        missing_ratios = [0]
    print('missing ratio list', missing_ratios)
    
    for missing_ratio in missing_ratios:
        
        """prepare for training"""
        n_runs = args.n_runs
        n_splits = args.n_splits
        subset = False

        acc_arr = np.zeros((n_splits, n_runs))
        auprc_arr = np.zeros((n_splits, n_runs))
        auroc_arr = np.zeros((n_splits, n_runs))
        precision_arr = np.zeros((n_splits, n_runs))
        recall_arr = np.zeros((n_splits, n_runs))
        F1_arr = np.zeros((n_splits, n_runs))
        rmse_arr = np.zeros((n_splits, n_runs))
        mape_arr = np.zeros((n_splits, n_runs))
        mae_arr = np.zeros((n_splits, n_runs))
        throughput_arr = np.zeros((n_splits, n_runs))
        memory_usage_arr = np.zeros((n_splits, n_runs))

        for k in range(5):

            split_idx = k + 1
            print('Split id: %d' % split_idx)
            if dataset == 'P12':
                if subset == True:
                    split_path = '/splits/phy12_split_subset' + str(split_idx) + '.npy'
                else:
                    split_path = '/splits/phy12_split' + str(split_idx) + '.npy'
            elif dataset == 'P19':
                split_path = '/splits/phy19_split' + str(split_idx) + '_new.npy'
            elif dataset == 'PAM':
                split_path = '/splits/PAMAP2_split_' + str(split_idx) + '.npy'
            else:
                split_path = '/splits/split_' + str(split_idx) + '.npy'
            
            # find the pretrained mim/mae image model 
            if args.finetune_mim:
                pretrained_image_model_dir = f"../../ckpt/ImgMIM/{dataset_prefix}{dataset}_{model}_{mask_patch_size}_{mask_ratio}_{mask_method}/split{split_idx}"
            elif args.finetune_mae:
                pretrained_image_model_dir = f"../../ckpt/ImgMAE/{dataset_prefix}{dataset}_{model}_{mask_ratio}_{mask_method}/split{split_idx}"
            else:
                pretrained_image_model_dir = None

            # load the pretrained mim/mae image model 
            if pretrained_image_model_dir:
                if os.path.exists(pretrained_image_model_dir):
                    dir_list = os.listdir(pretrained_image_model_dir) # find the latest checkpoint
                    latest_checkpoint_idx = 0
                    for d in dir_list:
                        if "checkpoint" in d:
                            checkpoint_idx = int(d.split("-")[-1])
                            if checkpoint_idx > latest_checkpoint_idx:
                                latest_checkpoint_idx = checkpoint_idx

                    if latest_checkpoint_idx > 0 and os.path.exists(os.path.join(pretrained_image_model_dir, f"checkpoint-{latest_checkpoint_idx}")):
                        model_path = os.path.join(pretrained_image_model_dir, f"checkpoint-{latest_checkpoint_idx}")
                    else:
                        raise Exception(f"{pretrained_image_model_dir} can't be found!")
                else:
                    raise Exception(f"{pretrained_image_model_dir} can't be found!")

            # the path to save models
            if args.output_dir is None:
                if args.train_from_scratch:
                    output_dir = f"../../ckpt/ImgCLS/{dataset_prefix}{dataset}_{model}_from_scratch/split{split_idx}"
                else:
                    if args.finetune_mim:
                        output_dir = f"../../ckpt/ImgCLS/{dataset_prefix}{dataset}_{model}_mim_{mask_patch_size}_{mask_ratio}_{mask_method}/split{split_idx}"
                    elif args.finetune_mae:
                        output_dir = f"../../ckpt/ImgCLS/{dataset_prefix}{dataset}_{model}_mae_{mask_ratio}_{mask_method}/split{split_idx}"
                    else:
                        output_dir = f"../../ckpt/ImgCLS/{dataset_prefix}{dataset}_{model}/split{split_idx}"
            else:
                output_dir = args.output_dir

            # prepare the data:
            Ptrain, Pval, Ptest, ytrain, yval, ytest = get_data_split(base_path, split_path, split_idx, dataset=dataset, prefix=dataset_prefix, 
                                                                      upsample=upsample, 
                                                                      missing_ratio=missing_ratio,
                                                                      feature_removal_level=feature_removal_level)
            print(len(Ptrain), len(Pval), len(Ptest), len(ytrain), len(yval), len(ytest))
            
            # if pval is none: use test dataset instead
            if len(Pval) == 0:
                Pval = Ptest
                yval = ytest
                load_best_model_at_end = False
                print("Don't have val dataset, use test dataset as eval dataset instead")
            else:
                load_best_model_at_end = True

            for m in range(n_runs):
                print('- - Run %d - -' % (m + 1))
                acc, precision, recall, F1, auc, aupr, rmse, mape, mae, throughput, memory_usage = fine_tune_hf(
                model_path=model_path,
                model_loader=model_loader,
                output_dir=output_dir,
                dataset=dataset,
                train_dataset=Ptrain,
                val_dataset=Pval,
                test_dataset=Ptest,
                image_size=image_size,
                grid_layout=grid_layout,
                window_size=window_size,
                num_classes=num_classes,
                epochs=epochs,
                train_batch_size=args.train_batch_size,
                eval_batch_size=args.eval_batch_size,
                logging_steps=args.logging_steps,
                save_steps=args.save_steps,
                learning_rate=args.learning_rate,
                seed=args.seed,
                save_total_limit=args.save_total_limit,
                load_best_model_at_end=load_best_model_at_end,
                do_train=args.do_train,
                continue_training=args.continue_training,
                train_from_scratch=args.train_from_scratch
                )

                test_report = 'Testing: Precision = %.2f | Recall = %.2f | F1 = %.2f\n' % (precision * 100, recall * 100, F1 * 100)
                test_report += 'Testing: AUROC = %.2f | AUPRC = %.2f | Accuracy = %.2f\n' % (0, 0, acc * 100)
                test_report += 'Testing: Throughput = %.2f samples/sec | Memory Usage = %.2f MB\n' % (throughput, memory_usage)
                print(test_report)
                
                if args.do_train: 
                    result_path = f"train_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                else: 
                    result_path = f"test_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                with open(os.path.join(output_dir, result_path), "w+") as f:
                    f.write(test_report)

                # store testing results
                acc_arr[k, m] = acc * 100
                auprc_arr[k, m] = aupr * 100
                auroc_arr[k, m] = auc * 100
                precision_arr[k, m] = precision * 100
                recall_arr[k, m] = recall * 100
                F1_arr[k, m] = F1 * 100
                rmse_arr[k, m] = rmse
                mape_arr[k, m] = mape
                mae_arr[k, m] = mae
                throughput_arr[k, m] = throughput
                memory_usage_arr[k, m] = memory_usage

        # pick best performer for each split based on max AUROC
        if dataset == "PAM":
            idx_max = np.argmax(acc_arr, axis=1)
        else:
            idx_max = np.argmax(auroc_arr, axis=1)
        acc_vec = [acc_arr[k, idx_max[k]] for k in range(n_splits)]
        auprc_vec = [auprc_arr[k, idx_max[k]] for k in range(n_splits)]
        auroc_vec = [auroc_arr[k, idx_max[k]] for k in range(n_splits)]
        precision_vec = [precision_arr[k, idx_max[k]] for k in range(n_splits)]
        recall_vec = [recall_arr[k, idx_max[k]] for k in range(n_splits)]
        F1_vec = [F1_arr[k, idx_max[k]] for k in range(n_splits)]
        rmse_vec = [rmse_arr[k, idx_max[k]] for k in range(n_splits)]
        mape_vec = [mape_arr[k, idx_max[k]] for k in range(n_splits)]
        mae_vec = [mae_arr[k, idx_max[k]] for k in range(n_splits)]
        throughput_vec = [throughput_arr[k, idx_max[k]] for k in range(n_splits)]
        memory_usage_vec = [memory_usage_arr[k, idx_max[k]] for k in range(n_splits)]

        mean_acc, std_acc = np.mean(acc_vec), np.std(acc_vec)
        mean_auprc, std_auprc = np.mean(auprc_vec), np.std(auprc_vec)
        mean_auroc, std_auroc = np.mean(auroc_vec), np.std(auroc_vec)
        mean_precision, std_precision = np.mean(precision_vec), np.std(precision_vec)
        mean_recall, std_recall = np.mean(recall_vec), np.std(recall_vec)
        mean_F1, std_F1 = np.mean(F1_vec), np.std(F1_vec)
        mean_rmse, std_rmse = np.mean(rmse_vec), np.std(rmse_vec)
        mean_mape, std_mape = np.mean(mape_vec), np.std(mape_vec)
        mean_mae, std_mae = np.mean(mae_vec), np.std(mae_vec)
        mean_throughput, std_throughput = np.mean(throughput_vec), np.std(throughput_vec)
        mean_memory_usage, std_memory_usage = np.mean(memory_usage_vec), np.std(memory_usage_vec)

        # printing the report
        test_report = "missing ratio:{}\n".format(missing_ratios)
        test_report += '------------------------------------------\n'
        test_report += 'Accuracy      = %.1f +/- %.1f\n' % (mean_acc, std_acc)
        test_report += 'AUPRC         = %.1f +/- %.1f\n' % (mean_auprc, std_auprc)
        test_report += 'AUROC         = %.1f +/- %.1f\n' % (mean_auroc, std_auroc)
        test_report += 'Precision     = %.1f +/- %.1f\n' % (mean_precision, std_precision)
        test_report += 'Recall        = %.1f +/- %.1f\n' % (mean_recall, std_recall)
        test_report += 'F1            = %.1f +/- %.1f\n' % (mean_F1, std_F1)
        test_report += 'RMSE          = %.1f +/- %.1f\n' % (mean_rmse, std_rmse)
        test_report += 'MAPE          = %.1f +/- %.1f\n' % (mean_mape, std_mape)
        test_report += 'MAE           = %.1f +/- %.1f\n' % (mean_mae, std_mae)
        test_report += 'Throughput    = %.1f +/- %.1f samples/sec\n' % (mean_throughput, std_throughput)
        test_report += 'Memory Usage  = %.1f +/- %.1f MB\n' % (mean_memory_usage, std_memory_usage)
        print(test_report)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        with open(os.path.join(output_dir.split("split")[0], f"test_result_{timestamp}.txt"), "w+") as f:
            f.write(test_report)

        if len(missing_ratios) > 1:
            _ = input(f"Current missing ratio: {missing_ratio}. \nPress ENTER to check out the next missing value:")

