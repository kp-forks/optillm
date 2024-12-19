import re
import cerebras
import openai

from dataclasses import dataclass
from typing import Optional, Literal

import yaml

@dataclass
class CepoConfig:
    bestofn_n: int = 3
    bestofn_temperature: float = 0.1
    bestofn_max_tokens: int = 4096
    bestofn_rating_type: Literal["absolute", "pairwise"] = "absolute"
    planning_n: int = 3
    planning_m: int = 6
    planning_temperature_step1: float = 0.55
    planning_temperature_step2: float = 0.25
    planning_temperature_step3: float = 0.1
    planning_temperature_step4: float = 0
    planning_max_tokens_step1: int = 4096
    planning_max_tokens_step2: int = 4096
    planning_max_tokens_step3: int = 4096
    planning_max_tokens_step4: int = 4096


# given command line arguments which includes a yaml file path, initialize a CePO configuration
def init_cepo_config(cmd_line_args: dict) -> CepoConfig:
    # get the command line arguments
    cepo_args = {
        key.split("cepo_")[1]: value
        for key, value in cmd_line_args.items()
        if "cepo" in key and "cepo_config_file" != key and value is not None
    }

    # get the yaml file arguments
    cepo_config_yaml = {}
    if "cepo_config_file" in cmd_line_args.keys():
        with open(cmd_line_args["cepo_config_file"], "r") as yaml_file:
            cepo_config_yaml = yaml.safe_load(yaml_file)

    # check if any of the keys overlap, and if they do, error out
    for key in cepo_config_yaml.keys():
        if key in cepo_args.keys():
            raise RuntimeError(f"Key {key} is found in both yaml file and command line arguments")

    # if not, then we take both of them and add them to the cepo config
    cepo_config = CepoConfig()
    cepo_attrs = [key for key, _ in cepo_config.__dict__.items() if not key.startswith('__')]

    # add command line arguments
    for key, value in cepo_args.items():
        # this assert should not be raised as the cli parser should catch this
        assert key in cepo_attrs, f"Command line argument {key} is not found in CepoConfig"
        setattr(cepo_config, key, value)

    # add yaml arguments
    for key, value in cepo_config_yaml.items():
        assert key in cepo_attrs, f"Yaml argument {key} is not found in CepoConfig"
        setattr(cepo_config, key, value)

    return cepo_config

def extract_question_only(task: str) -> str:
    """We noticed that sometimes if the task includes specific formatting instructions, they may interfere with the reasoning flow. This
    is a temporary workaround to extract the question only from the task. Work in progress.
    """
    question_only = task.replace('\n## Question: \n\n', '')
    question_only = question_only.replace('\n\n\n## Instruction \n\nPlease answer this question by first reasoning and then providing your answer.\nPresent your reasoning and solution in the following json format. \nPlease show your final answer in the `answer` field, e.g.,`"answer": "42"`.\n\n```json\n{\n    "reasoning": "___",\n    "answer": "___"\n}\n```\n', '')
    return question_only


def generate_completion(system_prompt: str, task: str, client, model: str, cepo_config: CepoConfig) -> str:
    completion_tokens = 0
    question_only = extract_question_only(task)
    cb_log = {}
    plans = []

    for i in range(cepo_config.planning_m):  # m is the maximum number of attempts to generate n plans
        # Step 1 - Generate a plan
        content = f"To answer this question, can you come up with a concise plan to solve it step-by-step but do not provide the "\
                  f"final answer. Also, for each step, provide your confidence in the correctness of that step as well as your ability "\
                  f"to execute it correctly. Here is the question:\n{question_only}\nRead the question again:\n\n{question_only}"

        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=cepo_config.planning_max_tokens_step1,
            temperature=cepo_config.planning_temperature_step1,
            stream=False,
        )
        completion_tokens += response.usage.completion_tokens

        if response.choices[0].finish_reason == "length":
            # Skipping plan generation due to exceeding the token budget. Usually it means the plan is incomplete.
            continue

        # Step 2 - Execute the plan
        content = f"Can you execute the above plan step-by-step to produce the final answer. "\
                  f"Be extra careful when executing steps where your confidence is lower."
        messages.extend([{"role": "assistant", "content": response.choices[0].message.content}, {"role": "user", "content": content}])
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=cepo_config.planning_max_tokens_step2,
            temperature=cepo_config.planning_temperature_step2,
            stream=False,
        )
        completion_tokens += response.usage.completion_tokens

        if response.choices[0].finish_reason == "length":
            messages.append({"role": "assistant", "content": response.choices[0].message.content})
            cb_log[f"messages_planning_{i}_rejected_due_to_length"] = messages
            continue

        plans.append(response.choices[0].message.content)
        messages.append({"role": "assistant", "content": response.choices[0].message.content})
        cb_log[f"messages_planning_{i}"] = messages
        
        if len(plans) == cepo_config.planning_n:
            break

    if not plans:
        # If no plans were generated succesfully, take the last one even if it was rejected due to length
        plans.append(response.choices[0].message.content)
        messages.append({"role": "assistant", "content": response.choices[0].message.content})
        cb_log[f"messages_planning_{i}_no_plans_so_taking_the_last_one"] = messages

    # Step 3 - Review and address inconsistencies
    try:
        plans_message = ""
        for i, plan in enumerate(plans):
            plans_message += f"Response {i + 1}:\n{plan}\n\n"
        plans_message = plans_message[:-2]  # remove the last 2x newline
        content = f"Can you review your last {len(plans)} responses and identify any inconsistency between them. After that, can you address "\
                  f"it and present a final step-by-step solution to the problem? Here is the question:\n{question_only}"
        messages = [{"role": "assistant", "content": plans_message}, {"role": "user", "content": content}]

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=cepo_config.planning_max_tokens_step3,
            temperature=cepo_config.planning_temperature_step3,
            stream=False,
        )
        final_solution = response.choices[0].message.content
        completion_tokens += response.usage.completion_tokens
    except (cerebras.cloud.sdk.BadRequestError, openai.BadRequestError) as e:
        # In case of an error, take the first plan as the final solution
        final_solution = plans[0]
        messages = []

    # Step 4 - Answer the question
    content = f"Use your final solution from above to correctly answer the question. Here is the question:\n{task}"
    messages = [{"role": "assistant", "content": final_solution}, {"role": "user", "content": content}]

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=cepo_config.planning_max_tokens_step4,
        temperature=cepo_config.planning_temperature_step4,
        stream=False,
    )
    completion_tokens += response.usage.completion_tokens

    cb_log["messages"] = messages
    return response.choices[0].message.content, completion_tokens, cb_log


def generate_n_completions(system_prompt: str, initial_query: str, client, model: str, cepo_config: CepoConfig) -> tuple[list[str], int, dict]:
    completion_tokens = 0
    cb_log = {}
    completions = []

    for i in range(cepo_config.bestofn_n):
        response_i, completion_tokens_i, cb_log_i = generate_completion(system_prompt, initial_query, client, model, cepo_config)
        completions.append(response_i)
        completion_tokens += completion_tokens_i
        cb_log[f"completion_{i}_response"] = response_i
        cb_log[f"completion_{i}_log"] = cb_log_i
        cb_log[f"completion_{i}_completion_tokens"] = completion_tokens_i    

    return completions, completion_tokens, cb_log


def rate_completions_absolute(system_prompt: str, initial_query: str, client, model: str, completions: list[str], cepo_config: CepoConfig, cb_log: dict) -> tuple[str, int, dict]:
    completion_tokens = 0
    rating_messages = [{"role": "system", "content": system_prompt},
                       {"role": "user", "content": initial_query}]
    content = "Please act as an impartial judge and evaluate the quality of the response provided by an AI assistant to "\
              "the user question displayed below. Your evaluation should consider correctness as a primary factor as "\
              "well as other factors such as the helpfulness, relevance, accuracy, depth, creativity, and level of "\
              "detail of the response. Evaluation Criteria:\n"\
              "- Correctness: How free is it from errors or mistakes?\n"\
              "- Helpfulness: How effectively does the response meet the user's needs?\n"\
              "- Relevance: How directly does the response address the original question?\n"\
              "- Accuracy: Are the information and explanations factually correct?\n"\
              "- Depth: Does the response provide comprehensive and meaningful insights?\n"\
              "- Creativity: Does the response offer unique or innovative perspectives?\n"\
              "- Clarity: Is the response well-organized, coherent, and easy to understand?\n"\
              "Evaluation Process:\n"\
              "1. Carefully review the user question and the AI assistant's response.\n"\
              "2. Assess the response against each criterion.\n"\
              "3. Provide a concise explanation of your overall evaluation.\n"\
              "4. Rate the response on a 1-10 scale with the following guidelines:\n"\
              "    - 1-2: Completely inadequate, fails to address the question\n"\
              "    - 3-4: Minimal relevance, significant deficiencies\n"\
              "    - 5-6: Partially helpful, requires substantial improvement\n"\
              "    - 7-8: Good response with minor areas for enhancement\n"\
              "    - 9-10: Correct, comprehensive, and highly insightful.\n"\
              "Begin your evaluation by providing a short explanation. Be as objective as possible. After providing your "\
              "explanation, please rate the response on a scale of 1 to 10 by strictly following this format:  \"Rating: "\
              "[[rating]]\", for example: \"Rating: [[5]]\""
    rating_messages.append({"role": "system", "content": content})
    
    ratings = []
    for i, completion in enumerate(completions):
        rating_messages.append({"role": "assistant", "content": completion})
        content = "Rate the above response beginning with a small evaluation blurb followed by a rating on a scale of 1 to 10 "\
                  "by strictly following this format: \"Explanation: <reason for your rating>\n\nRating: [[rating]]\"."
        rating_messages.append({"role": "system", "content": content})

        rating_response = client.chat.completions.create(
            model=model,
            messages=rating_messages,
            max_tokens=cepo_config.bestofn_max_tokens,
            temperature=cepo_config.bestofn_temperature
        )
        completion_tokens += rating_response.usage.completion_tokens
        
        rating_response = rating_response.choices[0].message.content.strip()
        cb_log[f"rating_response_{i}"] = rating_response

        pattern = r"Rating: \[\[(\d+)\]\]"
        match = re.search(pattern, rating_response)
        if match:
            rating_response = match.group(1)
        else:
            rating_response = "0"

        try:
            rating = float(rating_response)
            ratings.append(rating)
        except ValueError:
            ratings.append(0)
        
        rating_messages = rating_messages[:-2]
    
    best_index = ratings.index(max(ratings))
    cb_log["ratings"] = ratings
    cb_log["best_index"] = best_index
    return completions[best_index], completion_tokens, cb_log


def rate_completions_pairwise(system_prompt: str, initial_query: str, client, model: str, completions: list[str], cepo_config: CepoConfig, cb_log: dict) -> tuple[str, int, dict]:
    completion_tokens = 0
    rating_messages = [{"role": "system", "content": system_prompt},
                       {"role": "user", "content": initial_query}]
    content = "Please act as an impartial judge and compare the quality of the two responses provided by the AI assistant " \
              "to the user's question displayed below. Evaluation Criteria:\n" \
              "- Helpfulness: How effectively does the response meet the user's needs?\n" \
              "- Relevance: How directly does the response address the original question?\n" \
              "- Accuracy: Are the information and explanations factually correct?\n" \
              "- Depth: Does the response provide comprehensive and meaningful insights?\n" \
              "- Creativity: Does the response offer unique or innovative perspectives?\n" \
              "- Clarity: Is the response well-organized, coherent, and easy to understand?\n" \
              "Evaluation Process:\n" \
              "1. Carefully review the user's question and the AI assistant's responses.\n" \
              "2. Compare the responses against each other for each criterion.\n" \
              "3. Provide a concise explanation of your overall evaluation.\n" \
              "4. Select the response that is superior based on the above criteria.\n" \
              "Reply with \"Better Response: [[response id]]\".\n" \
              "If the first response is better, reply with \"Better Response: [[0]]\". " \
              "If the second response is better, reply with \"Better Response: [[1]]\"."
    rating_messages.append({"role": "system", "content": content})

    ratings = [0] * cepo_config.bestofn_n
    pairs = [(i, j) for i in range(cepo_config.bestofn_n) for j in range(cepo_config.bestofn_n) if i != j]
    for pair in pairs:
        responses_pair = f"Response 0: {completions[pair[0]]}\n\nResponse 1: {completions[pair[1]]}"
        rating_messages.append({"role": "assistant", "content": responses_pair})
        content =  "Reply with \"Better Response: [[response id]]\".\n" \
                   "If the first response is better, reply with \"Better Response: [[0]]\". " \
                   "If the second response is better, reply with \"Better Response: [[1]]\"."
        rating_messages.append({"role": "system", "content": content})

        rating_response = client.chat.completions.create(
            model=model,
            messages=rating_messages,
            max_tokens=cepo_config.bestofn_max_tokens,
            temperature=cepo_config.bestofn_temperature
        )
        completion_tokens += rating_response.usage.completion_tokens
        
        rating_response = rating_response.choices[0].message.content.strip()
        cb_log[f"rating_response_for_pair_{pair[0]}_{pair[1]}"] = rating_response

        pattern = r"Better Response: \[\[(\d+)\]\]"
        match = re.search(pattern, rating_response)
        if match:
            rating_response = match.group(1)
            try:
                rating = int(rating_response)
                ratings[pair[rating]] += 1
            except ValueError:
                ratings[pair[0]] += 1  # if parsing unsuccessful, default to the first response
        else:
            ratings[pair[0]] += 1  # if parsing unsuccessful, default to the first response

        rating_messages = rating_messages[:-2]
    
    best_index = ratings.index(max(ratings))
    cb_log["ratings"] = ratings
    cb_log["best_index"] = best_index
    return completions[best_index], completion_tokens, cb_log


def cepo(system_prompt: str, initial_query: str, client, model: str, cepo_config: Optional[CepoConfig] = None) -> list[str, int, dict]:
    if cepo_config is None:
        cepo_config = CepoConfig()
    
    # Generate completions
    completions, completion_tokens_planning, cb_log = generate_n_completions(system_prompt, initial_query, client, model, cepo_config)  # cb_log is a dictionary for debugging purposes
    
    # Rate the completions
    if cepo_config.bestofn_rating_type == "absolute":
        best_completion, completion_tokens_rating, cb_log = rate_completions_absolute(system_prompt, initial_query, client, model, completions, cepo_config, cb_log)
    elif cepo_config.bestofn_rating_type == "pairwise":
        best_completion, completion_tokens_rating, cb_log = rate_completions_pairwise(system_prompt, initial_query, client, model, completions, cepo_config, cb_log)
    else:
        raise ValueError("Invalid rating type in cepo_config")
    
    return best_completion, completion_tokens_planning + completion_tokens_rating
