import torch
from torch import cos, sin, arccos,arcsin, mean, sqrt, square
import os
import gcsfs
from requests import get
import numpy as np
import binascii
import collections
import datetime
import hashlib
import sys
from google.oauth2 import service_account
import six
from six.moves.urllib.parse import quote

# Storage
CRED_PATH = "/".join([os.getcwd(), 'utils', 'storage.json'])
fs = gcsfs.GCSFileSystem(token=CRED_PATH)
storage_options = {"token": CRED_PATH}
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CRED_PATH

# Operation
PREEMPT_URL = "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
PREEMPT_HEADER = {"Metadata-Flavor" : "Google"}
# Constants
PI = torch.acos(torch.zeros(1)).item() * 2
EARTH_RADIUS = 6371
DEG2RAD = PI/180


def save_model(model, model_path):
    torch.save(model, model_path)
    fs.upload(f'{model_path}/*', f"gs://geobert/checkpoints/{model_path}")


def handle_preempt(model, model_path):
    resp_text= get(PREEMPT_URL, headers=PREEMPT_HEADER).text
    if resp_text == 'FALSE':
        return False
    elif resp_text == 'TRUE':
        save_model(model, model_path)
        fs.upload(f'{model_path}/*', f"gs://geobert/checkpoints/{model_path}")
        return True


class GEODataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx], dtype=torch.float)
        return item

    def __len__(self):
        return len(self.labels)


class StreamTokenizedDataset(torch.utils.data.Dataset):
    def __init__(self, texts, labels, tokenizer, batchsize, max_seq_length):
        self.texts = texts
        self.labels = labels
        self.tokenizer = tokenizer
        self.batchsize = batchsize
        self.max_seq_length = max_seq_length
        self.batch = None

    def __getitem__(self, idx):
        self.requested_batch = int(idx/self.batchsize)
        if self.requested_batch != self.batch:
            self.batch = int(idx/self.batchsize)
            self.current_texts = self.texts[self.batchsize * self.batch: self.batchsize * self.batch + self.batchsize]
            self.encodings = self.tokenizer(self.current_texts, truncation=True, padding=True, max_length=self.max_seq_length)

        item = {key: torch.tensor(val[idx % self.batchsize]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx], dtype=torch.float)
        return item

    def __len__(self):
        return len(self.labels)


class PreemptibleIterationHandler():
    def __int__(self,
                model,
                n_iterations: int,
                start_iteration: int,
                n_epochs: int,
                start_epoch: int,
               train):
        self.model = model
        self.n_iterations = n_iterations
        self.start_iteration = start_iteration
        self.n_epochs = n_epochs
        self.start_epoch = start_epoch


class PreemptibleDataLoader():
    def _init_(self,
               dataset,
               batchsize: int,
               iteration: int):
        self.dataset = dataset
        self.batchsize = batchsize
        self.iteration = iteration
        self.startidx = self.batchsize * self.iteration
        
    def __getitem__(self, idx):
        endidx = min(len(self.dataset), self.startidx + self.batchsize)
        items = [self.dataset.__getitem__(i) for i in range(self.startidx, endidx)]
        self.startidx += self.batchsize
        keys = items[0].keys()
        return {key: torch.tensor([item[key] for item in items]) for key in keys} 
        
    def __len__(self):
        return (len(self.dataset) / self.batchsize) + 1


class MyNumbers:
    def __iter__(self):
        self.a = 1
        return self

    def __next__(self):
        if self.a <= 20:
            x = self.a
            self.a += 1
            return x
        else:
            raise StopIteration


def haversine_dist(logits, labels):
    ## phi = lat = index 0, lambda = lon = index 1
    labels = DEG2RAD * labels
    logits = DEG2RAD * logits
    d_sigma = 2 * (
        arcsin(
            sqrt(
                square(
                    sin(
                        (logits[:, 0]-labels[:, 0]) / 2)
                )
                +
                cos(logits[:, 0])
                *
                cos(labels[:, 0])
                *
                square(
                    sin(
                        (logits[:, 1]-labels[:, 1]) / 2)
                )
            )
        )
    )
    hav_dist = EARTH_RADIUS * d_sigma
    hav_dist_mean = mean(hav_dist)
    return hav_dist_mean


def generate_signed_url(service_account_file, bucket_name, object_name,
                        subresource=None, expiration=604800, http_method='GET',
                        query_parameters=None, headers=None):

    if expiration > 604800:
        print('Expiration Time can\'t be longer than 604800 seconds (7 days).')
        sys.exit(1)

    escaped_object_name = quote(six.ensure_binary(object_name), safe=b'/~')
    canonical_uri = '/{}'.format(escaped_object_name)

    datetime_now = datetime.datetime.now(tz=datetime.timezone.utc)
    request_timestamp = datetime_now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = datetime_now.strftime('%Y%m%d')

    google_credentials = service_account.Credentials.from_service_account_file(
        service_account_file)
    client_email = google_credentials.service_account_email
    credential_scope = '{}/auto/storage/goog4_request'.format(datestamp)
    credential = '{}/{}'.format(client_email, credential_scope)

    if headers is None:
        headers = dict()
    host = '{}.storage.googleapis.com'.format(bucket_name)
    headers['host'] = host

    canonical_headers = ''
    ordered_headers = collections.OrderedDict(sorted(headers.items()))
    for k, v in ordered_headers.items():
        lower_k = str(k).lower()
        strip_v = str(v).lower()
        canonical_headers += '{}:{}\n'.format(lower_k, strip_v)

    signed_headers = ''
    for k, _ in ordered_headers.items():
        lower_k = str(k).lower()
        signed_headers += '{};'.format(lower_k)
    signed_headers = signed_headers[:-1]  # remove trailing ';'

    if query_parameters is None:
        query_parameters = dict()
    query_parameters['X-Goog-Algorithm'] = 'GOOG4-RSA-SHA256'
    query_parameters['X-Goog-Credential'] = credential
    query_parameters['X-Goog-Date'] = request_timestamp
    query_parameters['X-Goog-Expires'] = expiration
    query_parameters['X-Goog-SignedHeaders'] = signed_headers
    if subresource:
        query_parameters[subresource] = ''

    canonical_query_string = ''
    ordered_query_parameters = collections.OrderedDict(
        sorted(query_parameters.items()))
    for k, v in ordered_query_parameters.items():
        encoded_k = quote(str(k), safe='')
        encoded_v = quote(str(v), safe='')
        canonical_query_string += '{}={}&'.format(encoded_k, encoded_v)
    canonical_query_string = canonical_query_string[:-1]  # remove trailing '&'

    canonical_request = '\n'.join([http_method,
                                   canonical_uri,
                                   canonical_query_string,
                                   canonical_headers,
                                   signed_headers,
                                   'UNSIGNED-PAYLOAD'])

    canonical_request_hash = hashlib.sha256(
        canonical_request.encode()).hexdigest()

    string_to_sign = '\n'.join(['GOOG4-RSA-SHA256',
                                request_timestamp,
                                credential_scope,
                                canonical_request_hash])

    # signer.sign() signs using RSA-SHA256 with PKCS1v15 padding
    signature = binascii.hexlify(
        google_credentials.signer.sign(string_to_sign)
    ).decode()

    scheme_and_host = '{}://{}'.format('https', host)
    signed_url = '{}{}?{}&x-goog-signature={}'.format(
        scheme_and_host, canonical_uri, canonical_query_string, signature)

    return signed_url


class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience."""
    def __init__(self, patience=7, verbose=False, delta=0, path='checkpoint.pt', trace_func=print):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 7
            verbose (bool): If True, prints a message for each validation loss improvement. 
                            Default: False
            delta (float): Minimum change in the monitored quantity to qualify as an improvement.
                            Default: 0
            path (str): Path for the checkpoint to be saved to.
                            Default: 'checkpoint.pt'
            trace_func (function): trace print function.
                            Default: print            
        """
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta
        self.path = path
        self.trace_func = trace_func
    def __call__(self, val_loss, model):

        score = -val_loss

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
        elif score < self.best_score + self.delta:
            self.counter += 1
            self.trace_func(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
            self.counter = 0

    def save_checkpoint(self, val_loss, model):
        '''Saves model when validation loss decrease.'''
        if self.verbose:
            self.trace_func(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
        torch.save(model, self.path)
        fs.upload(str(self.path), f"gs://geobert/" + str(self.path))
        self.val_loss_min = val_loss