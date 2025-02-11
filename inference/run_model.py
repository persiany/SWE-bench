import json
import os
import time
import dotenv
import traceback
from pathlib import Path
from tqdm.auto import tqdm
import numpy as np
import openai
from anthropic import HUMAN_PROMPT, AI_PROMPT, Anthropic
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)
from datasets import load_dataset, concatenate_datasets
from make_datasets.utils import extract_diff
from argparse import ArgumentParser
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
dotenv.load_dotenv()


MODEL_LIMITS = {
    'gpt-3.5-turbo-16k-0613': 16_000,
    'gpt-4-32k-0613': 31_000,
    'gpt-4-0613': 7_800,
}


MODEL_COST_PER_INPUT = {
    'gpt-3.5-turbo-16k-0613': 0.0000015,
    'gpt-4-0613': 0.00003,
    'gpt-4-32k-0613': 0.00006,
    'gpt-4': 0.00003,
    'gpt-35-turbo': 0.0000015,
    'gpt-4-32k': 0.00006,
}

 
MODEL_COST_PER_OUTPUT = {
    'gpt-3.5-turbo-16k-0613': 0.000002,
    'gpt-4-0613': 0.0006,
    'gpt-4-32k-0613': 0.00012,
    'gpt-4': 0.0006,
    'gpt-35-turbo': 0.000002,
    'gpt-4-32k': 0.00012,
}


ENGINES = {
    'gpt-3.5-turbo-16k-0613': 'gpt-35-turbo-16k',
    'gpt-4-0613': 'gpt-4',
    'gpt-4-32k-0613': 'gpt-4-32k',
}


def calc_cost(response):
    model_name = response.model
    input_tokens = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens
    cost = (
        MODEL_COST_PER_INPUT[model_name] * input_tokens
        + MODEL_COST_PER_OUTPUT[model_name] * output_tokens
    )
    logger.info(f'input_tokens={input_tokens}, output_tokens={output_tokens}, cost={cost:.2f}')
    return cost


@retry(wait=wait_random_exponential(min=30, max=600), stop=stop_after_attempt(3))
def call_chat(model_name_or_path, inputs, use_azure, temperature, top_p):
    system_messages = inputs.split("\n", 1)[0]
    user_message = inputs.split("\n", 1)[1]
    try:
        if use_azure:
            response = openai.ChatCompletion.create(
                engine=ENGINES[model_name_or_path] if use_azure else None,
                messages=[
                    {"role": "system", "content": system_messages},
                    {"role": "user", "content": user_message},
                ],
                temperature=temperature,
                top_p=top_p,
            )
        else:
            response = openai.ChatCompletion.create(
                model=model_name_or_path,
                messages=[
                    {"role": "system", "content": system_messages},
                    {"role": "user", "content": user_message},
                ],
                temperature=temperature,
                top_p=top_p,
            )
        cost = calc_cost(response)
        return response, cost
    except openai.error.InvalidRequestError as e:
        if e.code == 'context_length_exceeded':
            print("Context length exceeded")
            return None
        raise e


def openai_inference(
    test_dataset,
    model_name_or_path,
    output_file,
    model_args,
    existing_ids,
    max_cost,
):
    openai_key = os.environ.get("OPENAI_API_KEY", None)
    openai.api_key = openai_key
    print(f"Using OpenAI key {'*' * max(0, len(openai_key)-5) + openai_key[-5:]}")

    use_azure = model_args.pop("use_azure", False)
    if use_azure:
        openai.api_type = "azure"
        openai.api_base = "https://pnlpopenai3.openai.azure.com/"
        openai.api_version = '2023-05-15'

    temperature = model_args.pop('temperature', 0.2)
    top_p = model_args.pop('top_p', 0.95 if temperature > 0 else 1)
    print(f'Using temperature={temperature}, top_p={top_p}')

    basic_args = {
        "model_name_or_path": model_name_or_path,
    }
    total_cost = 0
    test_dataset = test_dataset.filter(lambda x: len(x['input_ids']) <= MODEL_LIMITS[model_name_or_path])
    print(f"Filtered to {len(test_dataset)} instances")
    with open(output_file, "a+") as f:
        for datum in tqdm(test_dataset, desc=f"Inference for {model_name_or_path}"):
            instance_id = datum["instance_id"]
            if instance_id in existing_ids:
                continue
            output_dict = {"instance_id": instance_id}
            output_dict.update(basic_args)
            output_dict["text"] = f"{datum['text']}\n\n"
            if len(datum['input_ids']) > MODEL_LIMITS[model_name_or_path]:
                output_dict["full_output"] = None
                output_dict["model_patch"] = None
            else:
                if model_name_or_path == 'gpt-4-32k-0613' and len(datum['input_ids']) <= 6000:
                    response, cost = call_chat('gpt-4-0613', output_dict["text"], use_azure, temperature, top_p)
                    completion = response.choices[0]['message']['content']
                else:
                    response, cost = call_chat(
                        output_dict["model_name_or_path"], output_dict["text"], use_azure, temperature, top_p
                    )
                    completion = response.choices[0]['message']['content']
                total_cost += cost
                print(f"Total Cost: {total_cost:.2f}")
                output_dict["full_output"] = completion
                output_dict["model_patch"] = extract_diff(completion)
            print(json.dumps(output_dict), file=f, flush=True)
            if max_cost is not None and total_cost >= max_cost:
                print(f"Reached max cost {max_cost}, exiting")
                break


@retry(wait=wait_random_exponential(min=60, max=600), stop=stop_after_attempt(6))
def call_anthropic(inputs, anthropic, model_name_or_path, temperature, top_p):
    try:
        completion = anthropic.completions.create(
            model=model_name_or_path,
            max_tokens_to_sample=6000,
            prompt=inputs,
            temperature=temperature,
            top_p=top_p,
        )
        return completion
    except Exception as e:
        logger.error(e)
        logger.error(f"Inputs: {inputs}")
        traceback.print_exc()
        time.sleep(20)
        return None


def anthropic_inference(
    test_dataset,
    model_name_or_path,
    output_file,
    model_args,
    existing_ids,
    max_cost,
):
    api_key = model_args.pop(
        "anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", None)
    )
    anthropic = Anthropic(api_key=api_key)
    temperature = model_args.pop('temperature', 0.2)
    top_p = model_args.pop('top_p', 0.95 if temperature > 0 else 1)
    print(f'Using temperature={temperature}, top_p={top_p}')

    basic_args = {
        "model_name_or_path": model_name_or_path,
    }
    with open(output_file, "a+") as f:
        for datum in tqdm(test_dataset, desc=f"Inference for {model_name_or_path}"):
            instance_id = datum["instance_id"]
            if instance_id in existing_ids:
                continue
            output_dict = {"instance_id": instance_id}
            output_dict.update(basic_args)
            output_dict["text_inputs"] = f"{HUMAN_PROMPT} {datum['text']}\n\n{AI_PROMPT}"
            completion = call_anthropic(output_dict["text_inputs"], anthropic, model_name_or_path, temperature, top_p)
            output_dict["full_output"] = completion.completion
            output_dict["model_patch"] = extract_diff(completion.completion)
            print(json.dumps(output_dict), file=f, flush=True)


def parse_model_args(model_args):
    kwargs = dict()
    if model_args is not None:
        for arg in model_args.split(","):
            key, value = arg.split("=")
            # infer value type
            if value in {"True", "False"}:
                kwargs[key] = value == "True"
            elif value.isnumeric():
                kwargs[key] = int(value)
            elif value.replace(".", "", 1).isnumeric():
                kwargs[key] = float(value)
            elif value in {"None"}:
                kwargs[key] = None
            elif value in {"[]"}:
                kwargs[key] = []
            elif value in {"{}"}:
                kwargs[key] = {}
            elif value.startswith("'") and value.endswith("'"):
                kwargs[key] = value[1:-1]
            elif value.startswith('"') and value.endswith('"'):
                kwargs[key] = value[1:-1]
            else:
                kwargs[key] = value
    return kwargs


def main(
    dataset_name,
    model_name_or_path,
    shard_id,
    num_shards,
    output_dir,
    model_args,
    max_cost,
):
    if shard_id is None and num_shards is not None:
        logger.warning(f"Received num_shards={num_shards} but shard_id is None, ignoring")
    if shard_id is not None and num_shards is None:
        logger.warning(f"Received shard_id={shard_id} but num_shards is None, ignoring")
    model_args = parse_model_args(model_args)
    model_nickname = model_name_or_path
    if "checkpoint" in Path(model_name_or_path).name:
        model_nickname = Path(model_name_or_path).parent.name
    else:
        model_nickname = Path(model_name_or_path).name
    output_file = f"{model_nickname}__{dataset_name.split('/')[-1]}"
    if shard_id is not None and num_shards is not None:
        output_file += f"__shard-{shard_id}__num_shards-{num_shards}"
    output_file = Path(output_dir, output_file + ".jsonl")
    logger.info(f"Will write to {output_file}")
    existing_ids = set()
    if os.path.exists(output_file):
        with open(output_file, 'r') as f:
            for line in f:
                data = json.loads(line)
                instance_id = data["instance_id"]
                existing_ids.add(instance_id)
    logger.info(f"Read {len(existing_ids)} already completed ids")
    dataset = load_dataset(dataset_name)
    load_splits = [split for split in dataset.keys() if 'test' in split]
    dataset = concatenate_datasets([dataset[split] for split in load_splits])
    if 'input_ids' in dataset.features:
        lens = np.array(list(map(len, dataset['input_ids'])))
    else:
        lens = np.array(list(map(len, dataset['text'])))
    dataset = dataset.select(np.argsort(lens))
    if len(existing_ids) > 0:
        dataset = dataset.filter(lambda x: x['instance_id'] not in existing_ids, desc="Filtering existing ids")
    if shard_id is not None and num_shards is not None:
        dataset = dataset.shard(num_shards, shard_id, contiguous=True)
    inference_args = {
        "test_dataset": dataset,
        "model_name_or_path": model_name_or_path,
        "output_file": output_file,
        "model_args": model_args,
        "existing_ids": existing_ids,
        "max_cost": max_cost,
    }
    if model_name_or_path in {"claude-2"}:
        anthropic_inference(**inference_args)
    elif model_name_or_path.startswith("gpt"):
        openai_inference(**inference_args)
    else:
        raise ValueError(f"Invalid model name or path {model_name_or_path}")
    logger.info(f"Done!")


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "--dataset_name",
        type=str,
        required=True,
        help="HuggingFace dataset name, with pre-tokenized inputs",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to the directory containing a lora or model",
    )
    parser.add_argument(
        "--shard_id",
        type=int,
        default=None,
        help="Shard id to process. If None, process all shards.",
    )
    parser.add_argument(
        "--num_shards",
        type=int,
        default=None,
        help="Number of shards. If None, process all shards.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        required=True,
        help="Path to the output file.",
    )
    parser.add_argument(
        "--model_args",
        type=str,
        default=None,
        help="List of model arguments separated by commas. (e.g. 'top_p=0.95,temperature=0.70')",
    )
    parser.add_argument(
        "--max_cost",
        type=float,
        default=None,
        help="Maximum cost to spend on inference.",
    )
    args = parser.parse_args()
    main(**vars(args))
