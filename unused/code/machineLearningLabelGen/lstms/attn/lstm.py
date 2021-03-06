# Note:
#   smaller batch size seems to make the tesitng accuracy better.

import os, time, random
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence, pad_packed_sequence

from Dataset import Dataset

torch.manual_seed(1209809284)
device = torch.device('cuda')
devcount = torch.cuda.device_count()
print('Device count:',devcount)
torch.cuda.set_device(devcount-1)
print('Current device:',torch.cuda.current_device())
print('Device name:',torch.cuda.get_device_name(devcount-1))

AllPossibleLabels = ['rightTO', 'lcRel', 'cutin', 'cutout', 'evtEnd', 'objTurnOff', 'end', 'NOLABEL']
NUMLABELS = len(AllPossibleLabels)

PREDICT_EVERY_NTH_FRAME = 15
WINDOWWIDTH = 30*16
INPUT_FEATURE_DIM = 42

BATCH_SIZE = 16
HIDDEN_DIM = 128
DROPOUT_RATE = .5
TEACHERFORCING_RATIO = 10 # to 1

RESUME_TRAINING = False
LOAD_CHECKPOINT_PATH = 'mostrecent.pt'

# 0 : rightTO
# 1 : lcRel
# 2 : cutin
# 3 : cutout
# 4 : evtEnd
# 5 : objTurnOff
# 6 : end
# 7 : NOLABEL

def clamp(l, h, x):
  if x < l: return l
  if x > h: return h
  return x

def oneHot(i):
  return torch.tensor([[[0. if j != i else 1. for j in range(NUMLABELS)]]], device=device)
oneHot = [oneHot(i) for i in range(len(AllPossibleLabels))]

def getBatch(dataset, aug=True, size=BATCH_SIZE):
  batch_xs,batch_xlengths,batch_ys = [],[],[]
  N = len(dataset)
  for i in range(size):
    # if i % 10 == 0:
    #   k = random.randint(0,N)
    # ((xs,xlengths),ys) = dataset[(i*4+k) % N]

    k = random.randint(0,N-1)
    ((xs,xlengths),ys) = dataset[k]

    # if aug:
    #   if random.randint(0,12412124) % 4 == 0:
    #     xs = [augment(x) for x in xs]
    batch_xs.extend(xs)
    batch_xlengths.extend(xlengths)
    batch_ys.extend(ys)
  batch_xs = pad_sequence(batch_xs).to(device=device)
  batch_xlengths = torch.tensor(batch_xlengths, device=device)
  batch_ys = torch.tensor(batch_ys, device=device)
  return (batch_xs,batch_xlengths),batch_ys

class Model(nn.Module):
  def __init__(self):
    super(Model, self).__init__()
    self.encoder = nn.LSTM(INPUT_FEATURE_DIM, HIDDEN_DIM)

    self.pencoder1 = nn.LSTM(HIDDEN_DIM*4, HIDDEN_DIM, bidirectional=True, batch_first=True)
    self.dropout1 = nn.Dropout(p=DROPOUT_RATE)

    self.pencoder2 = nn.LSTM(HIDDEN_DIM*4, HIDDEN_DIM, bidirectional=True, batch_first=True)
    self.dropout2 = nn.Dropout(p=DROPOUT_RATE)

    self.pencoder3 = nn.LSTM(HIDDEN_DIM*4, HIDDEN_DIM, bidirectional=True, batch_first=True)
    self.dropout3 = nn.Dropout(p=DROPOUT_RATE)

    self.pencoder4 = nn.LSTM(HIDDEN_DIM*4, HIDDEN_DIM, bidirectional=True, batch_first=True)
    self.dropout4 = nn.Dropout(p=DROPOUT_RATE)

    self.norm = torch.nn.LayerNorm(2*HIDDEN_DIM)

    self.attn = nn.Linear(HIDDEN_DIM*2 + NUMLABELS, WINDOWWIDTH//16)
    self.attnCombine = nn.Linear(HIDDEN_DIM*2 + NUMLABELS, HIDDEN_DIM)

    self.decoder = nn.GRU(HIDDEN_DIM, HIDDEN_DIM*2, batch_first=True)

    self.out = nn.Linear(HIDDEN_DIM*2, NUMLABELS)

  def encode(self, data):
    xs, xlengths = data

    batch_size = xs.shape[1] // WINDOWWIDTH
    packed_padded = pack_padded_sequence(xs, xlengths, enforce_sorted=False)
    packed_padded_out, hidden = self.encoder(packed_padded)
    # Check unpacked_lengths against xlengths to verify correct output ordering
    # unpacked_padded, unpacked_lengths = pad_packed_sequence(packed_padded_hidden[0])

    context_seq = torch.cat(hidden, dim=2) # (1, WINDOWWIDTH * BATCH_SIZE, 2*HIDDEN_DIM)

    context_seq = context_seq.reshape(batch_size, WINDOWWIDTH // 2, 4 * HIDDEN_DIM)
    context_seq = self.dropout1(context_seq)
    context_seq, _ = self.pencoder1(context_seq)

    context_seq = context_seq.reshape(batch_size, WINDOWWIDTH // 4, 4 * HIDDEN_DIM)
    context_seq = self.dropout2(context_seq)
    context_seq, _ = self.pencoder2(context_seq)

    context_seq = context_seq.reshape(batch_size, WINDOWWIDTH // 8, 4 * HIDDEN_DIM)
    context_seq = self.dropout3(context_seq)
    context_seq, _ = self.pencoder3(context_seq)

    context_seq = context_seq.reshape(batch_size, WINDOWWIDTH // 16, 4 * HIDDEN_DIM)
    context_seq = self.dropout4(context_seq)
    context_seq, hidden = self.pencoder4(context_seq)
    context_seq = self.norm(context_seq)

    hidden = hidden[0] # Take the h vector
    # hidden = (numdirections * layers, batch, hiddensize)
    hidden = hidden.transpose(1,0)
    hidden = hidden.reshape(batch_size, 2 * HIDDEN_DIM) # concats the forward & backward hiddens

    return context_seq, hidden

  def decoderStep(self, input, hidden, encoderOutputs):
    # print('input:',input.shape)
    # print('hidden:',hidden.shape)
    attnWeights = F.softmax(self.attn(torch.cat((input, hidden.unsqueeze(1)), dim=2)), dim=2)
    # print('attnWeights:',attnWeights.shape)
    # print('encoderOutputs:',encoderOutputs.shape)
    attnApplied = torch.bmm(attnWeights, encoderOutputs)
    # print('attnApplied:',attnApplied.shape)
    output = torch.cat((input, attnApplied), dim=2)
    output = self.attnCombine(output)
    # output = self.bn3(output.squeeze(1)).unsqueeze(1)
    output = F.relu(output)
    # print('relu:',output.shape)
    output, hidden = self.decoder(output, hidden.unsqueeze(0))
    hidden = hidden.squeeze(0)
    # print('decoder out:',output.shape)
    # print('decoder hid:',hidden.shape)
    output = F.log_softmax(self.out(output), dim=2)
    return output, hidden

  def forward(self, xs:torch.Tensor, ys:torch.Tensor=None):
    batch_size = xs[0].shape[1] // WINDOWWIDTH
    if ys is not None:
      ys = ys.view(batch_size, WINDOWWIDTH//PREDICT_EVERY_NTH_FRAME)
    context_seq, hidden = self.encode(xs)
    input = torch.zeros((batch_size,1,NUMLABELS), device=device)
    outputs = []
    for i in range(WINDOWWIDTH//PREDICT_EVERY_NTH_FRAME):
      output, hidden = self.decoderStep(input, hidden, context_seq)
      if ys is None:
        input = output
      else:
        input = torch.cat([oneHot[ys[j,i].item()] for j in range(batch_size)])
      outputs.append(output)
    output = torch.cat(outputs, dim=1)
    output = output.view(WINDOWWIDTH//PREDICT_EVERY_NTH_FRAME * batch_size, NUMLABELS)
    return output

  def beamDecode(self, data:torch.Tensor):
    context_seq, encoder_hidden = self.encode(data)
    beams = [] # tuple of (outputs, previous hidden, next input, beam log prob)
    batch_size = 1
  
    # get the initial beam
    input = torch.zeros((batch_size,1,NUMLABELS), device=device)
    output, hidden = self.decoderStep(input, encoder_hidden, context_seq)
    for i in range(NUMLABELS):
      beams.append(([i], hidden, oneHot[i], float(output[0,0,i])))
    for i in range(WINDOWWIDTH//PREDICT_EVERY_NTH_FRAME - 1):
      newBeams = []
      for beam in beams:
        outputs, hidden, input, beamLogProb = beam
        output, hidden = self.decoderStep(input, hidden, context_seq)
        for i in range(NUMLABELS):
          newBeam = (outputs + [i], hidden, oneHot[i], beamLogProb + float(output[0,0,i]))
          newBeams.append(newBeam)
      beams = sorted(newBeams, key=lambda x:-x[-1])[:NUMLABELS]
    outputs, _, _, _ = beams[0]
    return np.array([outputs])

def evaluate(model, lossFunction, sequences, saveFileName):
  start = time.time()

  outputs = []
  avgacc1 = 0
  avgacc2 = 0
  avgiou = 0

  for i in range(16):
    xs, ys = getBatch(sequences)
    yhats = model(xs).view(BATCH_SIZE, WINDOWWIDTH//PREDICT_EVERY_NTH_FRAME, NUMLABELS)
    yhats = yhats.argmax(dim=2).cpu().numpy()
    ys = ys.view(BATCH_SIZE, WINDOWWIDTH // PREDICT_EVERY_NTH_FRAME).cpu().numpy()
    for j in range(BATCH_SIZE):
      pred = ['_' if z == AllPossibleLabels.index('NOLABEL') else str(z) for z in yhats[j].tolist()]
      exp = ['.' if z == AllPossibleLabels.index('NOLABEL') else str(z) for z in ys[j].tolist()]
      a = set(yhats[j].tolist())
      b = set(ys[j].tolist())
      avgiou += len(a&b)/len(a|b)

      outputs.append(''.join(pred) + ' ' + ''.join(exp) + '\n\n')
    numlabels = (ys != AllPossibleLabels.index('NOLABEL')).sum()
    if numlabels > 0:
      avgacc1 += ((yhats == ys) & (ys != AllPossibleLabels.index('NOLABEL'))).sum() / numlabels
    avgacc2 += (yhats == ys).sum() / (BATCH_SIZE * WINDOWWIDTH // PREDICT_EVERY_NTH_FRAME)

  # Compare beam search with raw outputs
  batch_size = 1
  for i in range(8):
    xs, ys = getBatch(sequences, aug=False, size=batch_size)

    yhats = model(xs).view(batch_size, WINDOWWIDTH//PREDICT_EVERY_NTH_FRAME, NUMLABELS)
    yhats = yhats.argmax(dim=2).cpu().numpy()
    ys = ys.view(batch_size, WINDOWWIDTH // PREDICT_EVERY_NTH_FRAME).cpu().numpy()
    pred = ['_' if z == AllPossibleLabels.index('NOLABEL') else str(z) for z in yhats[0].tolist()]
    exp = ['.' if z == AllPossibleLabels.index('NOLABEL') else str(z) for z in ys[0].tolist()]
    output = ''.join(pred) + ' ' + ''.join(exp) + ' '

    yhats = model.beamDecode(xs)

    pred = ['_' if z == AllPossibleLabels.index('NOLABEL') else str(z) for z in yhats[0].tolist()]
    output = output +  ''.join(pred) + ' beam\n\n'
    outputs.append(output)

  end = time.time()
  with open(saveFileName, 'w') as f:
    f.write('acc:' + str(avgacc1/16) + ' iou:' + str(avgiou/16) + ' evaltime:'+str(int(end-start)) + '\n\n')
    f.writelines(outputs)

  return avgacc1 / 16, avgacc2 / 16, avgiou / 16

def checkpoint(trainloss,losses, model, optimizer, lossFunction, trainData, testData):
  model.eval()
  with torch.no_grad():
    avgtrainacc, avgtrainacc2, avgtrainiou = evaluate(model, lossFunction, trainData, 'trainOutputs.txt')
    avgtestacc, avgtestacc2, avgtestiou = evaluate(model, lossFunction, testData, 'testOutputs.txt')
    if len(losses.testAcc) and avgtestacc > max(losses.testAcc):
      torch.save((model.state_dict(), optimizer.state_dict()), 'maxacc.pt')
      torch.save(losses, 'maxacc_losses.pt')
    if len(losses.testIou) and avgtestiou > max(losses.testIou):
      torch.save((model.state_dict(), optimizer.state_dict()), 'maxiou.pt')
      torch.save(losses, 'maxiou_losses.pt')
    torch.save((model.state_dict(), optimizer.state_dict()), 'mostrecent.pt')
    torch.save(losses, 'mostrecent_losses.pt')
    losses.trainLoss.append(trainloss)
    losses.trainAcc.append(avgtrainacc)
    losses.testAcc.append(avgtestacc)
    losses.trainAcc2.append(avgtrainacc2)
    losses.testAcc2.append(avgtestacc2)
    losses.trainIou.append(avgtrainiou)
    losses.testIou.append(avgtestiou)
  model.train()
  return avgtestacc, avgtestiou

class Losses:
  def __init__(self):
    self.trainLoss = []
    self.testLoss = []
    self.trainAcc = []
    self.testAcc = []
    self.trainAcc2 = []
    self.testAcc2 = []
    self.trainIou = []
    self.testIou = []

def train(trainData, testData):
  (_,_,classCounts) = trainData.getStats()

  print('Training')
  model = Model()
  model.to(device)
  print(model)

  N = len(trainData)
  print('Train set num data:',N)
  print('Test set num data:',len(testData))
  print('Class counts:')
  for label,count in classCounts.items():
    print('\t',label,':',count)

  print('classCounts:',classCounts)
  classWeights = [1/(classCounts[lab]+1) for lab in range(NUMLABELS)] # order is important here
  classWeights = torch.tensor(classWeights, device=device) / sum(classWeights)

  lossFunction = nn.NLLLoss(weight=classWeights)
  optimizer = optim.Adam(model.parameters(), lr=.0005, weight_decay=.00001)

  losses = Losses()

  if RESUME_TRAINING:
    print('Resuming from checkpoint')

    (model_state, optimizer_state) = torch.load(LOAD_CHECKPOINT_PATH)

    model.load_state_dict(model_state)
    optimizer.load_state_dict(optimizer_state)
    optimizer.zero_grad()

  model.train()

  print('Enter training loop')
  avgloss = 0
  now = time.time()
  prev = now
  i = 0
  while True:
    xs,ys = getBatch(trainData)
    if i % TEACHERFORCING_RATIO:
      loss = lossFunction(model(xs,ys), ys)
    else:
      loss = lossFunction(model(xs), ys)
    avgloss += .1 * (float(loss) - avgloss)
    loss.backward()
    optimizer.step()
    optimizer.zero_grad()
    i += 1

    now = time.time()
    if now - prev > 60:
      prev = now
      testacc, testiou = checkpoint(avgloss, losses, model, optimizer, lossFunction, trainData, testData)
      print('trainloss {:1.5} i/s {:1.5} testacc {:1.5} testiou {:1.5}'.format(avgloss, i/60, testacc, testiou))
      i = 0

if __name__ == '__main__':
  trainData = Dataset(loadPath='../trainDataset.pkl')
  testData = Dataset(loadPath='../testDataset.pkl')
  train(trainData, testData)

def augment(t):
  tens = t.clone()
  i = random.randint(0,123521) % 3
  if i == 0:
    tens[0,1], tens[0,3] = tens[0,3], tens[0,1]
    tens[0,1] *= -1
    tens[0,3] *= -1
    tens[0,7], tens[0,8] = tens[0,8], tens[0,7]
    tens[0,7] *= -1
    tens[0,8] *= -1
    tens[0,12], tens[0,13] = tens[0,13], tens[0,12]
    tens[0,15], tens[0,17] = tens[0,17], tens[0,15]
    tens[0,-20:-10], tens[0,-10:] = tens[0,-10:], tens[0,-20:-10]
    tens[0,-20:] *= -1
  elif i == 1:
    tens[0,1:12] += torch.randn_like(tens[0,1:12]) * .2
    tens[0,-20:] += torch.randn_like(tens[0,-20:]) * .2
  else:
    tens += torch.randn_like(tens) * .1
  return tens

