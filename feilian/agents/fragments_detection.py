import re
import tiktoken
import json
import os
import json_repair
from typing_extensions import TypedDict
from typing import List, Optional, Annotated, Dict
from enum import Enum
from lxml import etree
from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from copy import deepcopy
from markdownify import MarkdownConverter

from feilian.etree_tools import (
    parse_html,
    clean_html,
    to_string,
    deduplicate_to_prune,
    extraction_based_pruning,
)
from feilian.etree_token_stats import extract_fragments_by_weight
from feilian.text_tools import convert_html_to_text
from feilian.prompts import (
    EXTRACTION_PROMPT_CN,
    EXTRACTION_PROMPT_HISTORY,
    BEST_ANSWERS_PROMPT_CN,
    BEST_COMPOSITION_CN,
)
from feilian.agents.reducers import merge_operators
from feilian.tools import format_to_ordered_list


encoder = tiktoken.encoding_for_model("gpt-4")
converter = MarkdownConverter()


class OperatorTypes(Enum):
    PRUNE = "prune"
    EXTRACT = "extract"


class Operator(TypedDict):
    xpath: str
    operator_type: Optional[OperatorTypes]
    text: Optional[str]
    data: Optional[Dict]


class Snippet(TypedDict):
    id: str
    raw_html: str
    ops: Annotated[List[Operator], merge_operators]


class FragmentDetectionState(Snippet):
    query: str


def tokenizer(text):
    if not text:
        return 0
    return len(encoder.encode(text))


def run_operators(tree, ops: List[Operator]) -> etree._Element:
    # 2. run prune operations
    prune_ops = [o for o in ops if o["operator_type"] == OperatorTypes.PRUNE]
    xpaths = deduplicate_to_prune([op["xpath"] for op in prune_ops])
    for xpath in xpaths:
        nodes: etree._Element = tree.xpath(xpath)
        if isinstance(nodes, list):
            for node in nodes:
                node.clear()
                node.text = ""
        else:
            nodes.clear()
            nodes.text = ""

    # 3. run extract operations
    extract_ops = [o for o in ops if o["operator_type"] == OperatorTypes.EXTRACT]
    xpaths = [op["xpath"] for op in extract_ops]
    extraction_based_pruning(tree, xpaths)

    return tree


def extract_fragments_node(state: FragmentDetectionState) -> FragmentDetectionState:
    ops = []
    tree = parse_html(state["raw_html"])
    clean_html(tree)

    for xpath in extract_fragments_by_weight(tree, tokenizer):
        nodes = tree.xpath(xpath)

        node_texts = []
        n: etree._Element
        for n in nodes:
            html_fragment = to_string(n)
            text = convert_html_to_text(html_fragment)
            node_texts.append(text)
            n.clear()
            n.text = ""

        text = "\n".join(node_texts)
        ops.append({"xpath": xpath, "text": text})

        print(f"[Fragment:{state['id']}] xpath: {xpath}, tokens: {tokenizer(text)}")

    # add /html to ops
    ops.append({"xpath": "/html", "text": convert_html_to_text(to_string(tree))})

    return {
        "id": state["id"],
        "raw_html": state["raw_html"],
        "ops": ops,
        "query": state["query"],
    }


def _create_extraction_chain():
    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL"),
        temperature=0,
        model_kwargs={
            "response_format": {
                "type": "json_object",
            },
        },
    )
    return EXTRACTION_PROMPT_CN.partial(chat_history=EXTRACTION_PROMPT_HISTORY) | llm


extraction_chain = _create_extraction_chain()


def detect_fragment_node(state: FragmentDetectionState) -> FragmentDetectionState:
    operator = state["ops"][0]
    if operator["text"] and operator["text"].strip():
        response = extraction_chain.invoke(
            {
                "context": operator["text"],
                "query": state["query"],
            }
        )
        data = json_repair.loads(response.content)
        data = {k: v for k, v in data.items() if k != "_thought" and v}
        print(
            f"[Detection:{state['id']}] xpath: {operator['xpath']}, extracted: {data}"
        )
        return {
            "ops": [{"xpath": operator["xpath"], "data": data}],
        }
    return {"ops": [{"xpath": operator["xpath"], "data": {}}]}


def _create_best_answer_chain():
    def parser(response):
        choices = response.content.strip()
        choices = [int(x) for x in choices.split(",")]
        return choices

    llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL"), temperature=0)
    return BEST_ANSWERS_PROMPT_CN | llm | parser


def _create_best_composition_chain():
    def parser(response):
        choices = response.content.split("最终结论:")[1].strip()
        choices = choices.split("\n")[0]
        try:
            choices = [int(x.strip()) for x in choices.split(",")]
        except Exception:
            # using regex to extract all numbers
            choices = [int(x) for x in re.findall(r"\d+", choices)]

        return choices

    llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL"), temperature=0)
    prompt = PromptTemplate.from_template(BEST_COMPOSITION_CN)
    return prompt | llm | parser


best_answer_chain = _create_best_answer_chain()
best_composition_chain = _create_best_composition_chain()


def classify_fragments_node(state: FragmentDetectionState) -> FragmentDetectionState:
    ops = deepcopy(state["ops"])

    for op in ops:
        keys = set(op["data"].keys())
        if len(keys) > 0:
            op["operator_type"] = OperatorTypes.EXTRACT
        else:
            op["operator_type"] = OperatorTypes.PRUNE

    # convert choices
    extract_ops = [op for op in ops if op["operator_type"] == OperatorTypes.EXTRACT]
    choices = [json.dumps(op["data"]) for op in extract_ops]

    # call llm to classify if there are multiple choices
    if len(choices) > 1:
        prune_ops = []
        extract_xpaths = [x["xpath"] for x in extract_ops]
        for op in ops:
            if op["operator_type"] == OperatorTypes.PRUNE:
                if any([x.startswith(op["xpath"]) for x in extract_xpaths]):
                    continue
                prune_ops.append(op)

        tree = parse_html(state["raw_html"])
        clean_html(tree)
        run_operators(tree, prune_ops)
        context = converter.convert(to_string(tree))

        choice_str = format_to_ordered_list(choices)
        best_choices = best_composition_chain.invoke(
            {
                "context": context,
                "query": state["query"],
                "choices": choice_str,
            }
        )
        best_choices = [x - 1 for x in best_choices]
    else:
        best_choices = [0] if extract_ops else []

    extract_op = [extract_ops[i] for i in best_choices]
    set_of_extract_xpath = set([op["xpath"] for op in extract_op])
    data = {}
    for op in extract_op:
        data.update(op["data"])
    print(
        f"[Classification:{state['id']}] best choices: {set_of_extract_xpath}, data: {data}"
    )

    # prune fragments
    output_ops = []
    for op in ops:
        if (
            op["operator_type"] == OperatorTypes.EXTRACT
            and op["xpath"] not in set_of_extract_xpath
        ):
            op["operator_type"] = OperatorTypes.PRUNE

        if op["operator_type"] == OperatorTypes.PRUNE and any(
            [x.startswith(op["xpath"]) for x in set_of_extract_xpath]
        ):
            continue

        del op["data"]
        output_ops.append(op)

    return {"ops": output_ops}


def fanout_to_fragment_detection(
    state: FragmentDetectionState,
) -> List[FragmentDetectionState]:
    ops = state["ops"]
    return [
        Send(
            "detect_fragment",
            {
                "id": state["id"],
                "raw_html": state["raw_html"],
                "ops": [op],
                "query": state["query"],
            },
        )
        for op in ops
    ]


def build_graph():
    builder = StateGraph(FragmentDetectionState)

    builder.add_node("extract_fragments", extract_fragments_node)
    builder.add_node("detect_fragment", detect_fragment_node)
    builder.add_node("classify_fragments", classify_fragments_node)

    builder.add_edge(START, "extract_fragments")
    builder.add_conditional_edges("extract_fragments", fanout_to_fragment_detection)
    builder.add_edge("detect_fragment", "classify_fragments")
    builder.add_edge("classify_fragments", END)

    return builder.compile()


fragment_detection_graph = build_graph()
