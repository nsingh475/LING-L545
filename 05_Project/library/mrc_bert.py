import torch
from transformers import BertTokenizer
from transformers import BertForQuestionAnswering
from transformers import AdamW

import copy
import pickle
import pandas as pd

class Generating_BERT_encodings():

    """Generate BERT encodings and add start and end positions of answer span for training data"""

    def __init__(self, df, batch_size, tokenizer_name, file_name, out_path, chunk_size):
        super().__init__()
        self.df = df
        self.batch_size = batch_size
        self.tokenizer_name = tokenizer_name
        self.file_name = file_name
        self.out_path = out_path
        self.chunk_size = chunk_size
        
    def run(self):
        ## ------------------------------------- Helper functions ------------------------------------------------------------##
        def divide_list_into_chunks(full_list, batch_size):
            for i in range(0, len(full_list), batch_size):
                yield full_list[i:i + batch_size]

        def write_data_to_pickle(data, file_name, path):
            with open(path+file_name, 'wb') as f:
                pickle.dump(data, f)
                
        
        ## start and end token position for bert
        def add_token_positions(encodings, answers):
            # initialize lists to contain the token indices of answer start/end
            start_positions = []
            end_positions = []
            enc = encodings['input_ids']

            for i in range(len(enc)):
                tokens = tokenizer.convert_ids_to_tokens(enc[i])

                gt_ans = answers[i]['text'].lower()
                gt_ans_tokens = gt_ans.split()
                len_gt_ans = len(gt_ans_tokens)

                start_gt_ans = gt_ans_tokens[0]
                if len_gt_ans>1:
                    end_gt_ans =  gt_ans_tokens[-1]

                def get_ind(tokens, ans_copy):
                    ind = -1
                    split_flag = False  ## to keep a track of splitting of word as in playing = play, ##ing
                    while ind == -1:
                        if ans_copy in tokens:
                            ind = tokens.index(ans_copy)
                        else:
                            if len(ans_copy)>0: ## to avoid inf loop in ans_copy = ans_copy[:-1]
                                ans_copy = ans_copy[:-1]
                                split_flag = True
                            else:
                                return -1  ## when exact word not found
                    if split_flag == True:
                        return ind+1  ## because the ans was split.

                    return ind

                start_ind = get_ind(tokens, copy.copy(start_gt_ans))
                if len_gt_ans>1:
                    end_ind = get_ind(tokens, copy.copy(end_gt_ans))   
                else:
                    end_ind = start_ind 

                start_positions.append(start_ind) 
                end_positions.append(end_ind)

                ## update our encodings object with the new token-based start/end positions
                encodings.update({'start_positions': start_positions, 'end_positions': end_positions})
 
        
        ## ------------------------------------- Main Execution ------------------------------------------------------------##
        
        ## tokenize question and context in batches
        c = list(self.df['Context'].values)
        q = list(self.df['Question'].values)
        a = list(self.df['Answer'].values)
        
        batch_context = list(divide_list_into_chunks(c, self.batch_size))
        batch_answers = list(divide_list_into_chunks(a, self.batch_size))
        batch_questions = list(divide_list_into_chunks(q, self.batch_size))
        
        tokenizer = BertTokenizer.from_pretrained(self.tokenizer_name)
        
        batch_encoding = []
        for i in range(len(batch_context)):
            encoding = tokenizer(batch_questions[i], batch_context[i], truncation=True, padding='max_length', max_length=512, return_tensors='pt')  ## generating the encoding
            batch_encoding.append(encoding) ## collecting_encoding
            
        ## updating encoding in batches
        for i in range(len(batch_encoding)):
            add_token_positions(batch_encoding[i], batch_answers[i])

        ## write batch encoding in chunks
        if self.chunk_size > 0:
            chunked_enodings = list(divide_list_into_chunks(batch_encoding, self.chunk_size))
            for num, chunk in enumerate(chunked_enodings):
                write_data_to_pickle(chunk, self.file_name+f'_{num+1}.pickle', self.out_path)
        else:
            num = 1 ## no chunking for test and dev data
            write_data_to_pickle(batch_encoding, self.file_name+f'_{num}.pickle', self.out_path)
        return  batch_encoding


class Transform_wikiSQLDataset(torch.utils.data.Dataset):

    """Transform encodings to tensor"""

    def __init__(self, encodings):
        self.encodings = encodings

    def __getitem__(self, idx):
        return {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}

    def __len__(self):
        return len(self.encodings.input_ids)


class Train_BERT_MRC():

    """Train model to learn to extract answer span from the context"""
    
    def __init__(self, out_path, batch_loader, freeze_layer_count, model_name, save_model_name, lr, iterations_before_saving_model, mdl):
        super().__init__()
        self.out_path = out_path
        self.batch_loader = batch_loader
        self.freeze_layer_count = freeze_layer_count
        self.model_name = model_name
        self.lr = lr
        self.save_model_name = save_model_name
        self.iterations_before_saving_model = iterations_before_saving_model
        self.mdl = mdl


        
    def run(self):
        ## ------------------------------------- Helper functions ------------------------------------------------------------##
        def initialize_model(model_name, freeze_layer_count):
            model = BertForQuestionAnswering.from_pretrained(model_name)
            
            if freeze_layer_count: ## freezing some bert layers
                for param in model.bert.embeddings.parameters(): ## Freeze the embeddings of the model
                    param.requires_grad = False
                if freeze_layer_count != -1: # if freeze_layer_count == -1, we only freeze the embedding layer otherwise we freeze the first `freeze_layer_count` encoder layers
                    for layer in model.bert.encoder.layer[:freeze_layer_count]:
                        for param in layer.parameters():
                            param.requires_grad = False
            return model
        
        ## ------------------------------------- Main Execution ------------------------------------------------------------##
        
        ## initializing model
        if self.mdl == None:
            model = initialize_model(self.model_name, self.freeze_layer_count)
        else:
            model = self.mdl
        
        device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
        model.to(device)
        model.train()
        optim = AdamW(model.parameters(), self.lr)
        
        ## training
        outputs_model = []
        print('Starting Training ...')
        for i , loader in enumerate(self.batch_loader):
            print(f'Processing batch {i+1}...')
            for batch in loader: 
                optim.zero_grad()
                input_ids = batch['input_ids'].to(device)
                attention_mask = batch['attention_mask'].to(device)
                start_positions = batch['start_positions'].to(device)
                end_positions = batch['end_positions'].to(device)

                outputs = model(input_ids, attention_mask=attention_mask, start_positions=start_positions,end_positions=end_positions)
                if len(outputs_model) == 0:
                    outputs_model.append(outputs)

                loss = outputs[0]
                loss.backward()
                optim.step()

            ## save intermediate model after processing n batches 
            if (i==0) or ((i+1)%self.iterations_before_saving_model==0):
                print(f'Saving intermediate BERT after processing {i+1} loaders') 
                torch.save(model, self.out_path+f'intermediate-bert')    

        ## saving the final model
        print('Saving the final model !!!')
        torch.save(model, self.out_path+self.save_model_name)
        
        return  model, outputs_model
    
class Evaluate_BERT_MRC():
    
    def __init__(self, model_path, model_name, batch_loader):
        super().__init__()
        self.model_path = model_path
        self.model_name = model_name
        self.batch_loader = batch_loader


        
    def run(self):
        ## ------------------------------------- Helper functions ------------------------------------------------------------##
        ## ------------------------------------- Main Execution ------------------------------------------------------------##
        
        ## initializing model
        model = torch.load(self.model_path+self.model_name)
        
        device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
        model.to(device)
        model.eval()
        
        ## evaluating
        acc = []
        dump_count = 0
        eval_data = []
        for loader in self.batch_loader:
            for batch in loader:
                with torch.no_grad():
                    input_ids = batch['input_ids'].to(device)
                    attention_mask = batch['attention_mask'].to(device)
                    start_true = batch['start_positions'].to(device)
                    end_true = batch['end_positions'].to(device)

                    outputs = model(input_ids, attention_mask=attention_mask) # make predictions

                    # get top prediction with argmax
                    start_pred = torch.argmax(outputs['start_logits'], dim=1)
                    end_pred = torch.argmax(outputs['end_logits'], dim=1)

                    # calculate accuracy 
                    acc.append(((start_pred == start_true).sum()/len(start_pred)).item())
                    acc.append(((end_pred == end_true).sum()/len(end_pred)).item())
                    eval_data.append( [start_pred, start_true, end_pred, end_true])

                    
        # calculate  accuracy in total
        acc = sum(acc)/len(acc)     
        return acc ,eval_data