from struct import unpack
from zlib import decompress
import sys
import re
import hashlib
import bs4
from dataclasses import dataclass
from torch.utils.data import Dataset
from transformers import PreTrainedTokenizer
import logging
import os
import torch
import pickle
from typing import Optional, List

logger = logging.getLogger(__name__)


# Helpers for beautiful soup
def find_at_most_one(bs, *args, **kwargs):
    t = bs.find_all(*args, **kwargs)
    if not t:
        return t
    elif len(t) > 1:
        raise InvalidParseAssumptionError("Too many found!")
    else:
        return t[0]


def find_exactly_one(bs, *args, **kwargs):
    t = bs.find_all(*args, **kwargs)
    if not t:
        raise InvalidParseAssumptionError("Not enough tags found!")
    elif len(t) > 1:
        raise InvalidParseAssumptionError("Too many found!")
    else:
        return t[0]


def find_at_least_one(bs, *args, **kwargs):
    t = bs.find_all(*args, **kwargs)
    if not t:
        raise InvalidParseAssumption("Not enough tags found!")
    return t


class InvalidParseAssumptionError(RuntimeError):
    pass


@dataclass
class Pronounciation:
    text: str
    type: str


@dataclass
class Definition:
    pos_modifier: Optional[str]
    definition: str
    examples: List[str]
    topic: Optional[str]
    date: Optional[str]


@dataclass
class ReferenceDefinition:
    pos_modifier: Optional[str]
    reference: str


@dataclass
class Sense:
    pos: Optional[str]
    definitions: List[Definition]


@dataclass
class Entry:
    word: str
    variant: Optional[int]
    senses: List[Sense]
    pronounciations: List[Pronounciation]
    phrases: List[Definition]
    origin: Optional[str]
    derivatives: List[str]


@dataclass
class DictionaryDefinition:
    title: str
    entry_str: str
    parsed_entry: Optional[bs4.BeautifulSoup] = None

    @classmethod
    def gen_from_apple_dictionary(cls, f):
        f.seek(0x40)
        limit = 0x40 + unpack("i", f.read(4))[0]
        f.seek(0x60)
        while f.tell() < limit:
            (sz,) = unpack("i", f.read(4))
            buf = decompress(f.read(sz)[8:])
            for m in re.finditer(b"<d:entry[^\n]+", buf):
                entry = m.group().decode()
                title = re.search('d:title="(.*?)"', entry).group(1)
                title_soup = bs4.BeautifulSoup(title, features="html.parser")
                entry_soup = bs4.BeautifulSoup(entry, features="html.parser")

                title = title_soup.get_text()
                entry = entry_soup.get_text()

                if not title or not entry:
                    logger.warning(f"Invalid entry {title}: {entry}")
                    continue

                yield cls(
                    title=title_soup.get_text(), entry_str=entry_soup.get_text(), parsed_entry=entry_soup,
                )


class AppleDictParser:
    @classmethod
    def parse_pronounciations(cls, parsed_entry):
        pronounciation_enclose = find_at_most_one(parsed_entry, "span", class_="prx") or find_at_most_one(
            parsed_entry, "span", class_="pr"
        )

        if not pronounciation_enclose or not pronounciation_enclose.get_text().strip():
            return None

        pronounciations = pronounciation_enclose("span", class_="ph")
        if not pronounciations:
            raise InvalidParseAssumptionError(f"No pronounciations found")

        p_dict = [Pronounciation(text=p.get_text(), type=p["d:pr"]) for p in pronounciations]
        return p_dict

    @classmethod
    def parse_sense_definitions(cls, parsed_entry):
        global_pos_modifier_span = parsed_entry.find_all("span", class_="gg")
        global_pos_modifier_span = [
            e for e in global_pos_modifier_span if not e.find_parents("span", class_="msDict")
        ]  # Filter out local ones
        global_pos_modifier = global_pos_modifier_span[0].get_text().strip() if global_pos_modifier_span else None
        definitions = []
        entry_spans = find_at_least_one(parsed_entry, "span", class_="msDict")
        for entry_span in entry_spans:
            definition_spans = entry_span.find_all("span", class_="df")
            if len(definition_spans) > 1:
                raise InvalidParseAssumptionError(f"Too many definitions for word!")
            elif definition_spans:
                definition_span = definition_spans[0]
                example_spans = entry_span("span", class_="ex")
                topic_span = find_at_most_one(entry_span, "span", class_="lg")
                local_pos_modifier_span = entry_span.find("span", class_="gg", recursive=False)
                local_pos_modifier = local_pos_modifier_span and local_pos_modifier_span.get_text().strip()

                definition = definition_span.get_text().strip()
                examples = [e.get_text().strip().strip(":").strip() for e in example_spans]
                topic = topic_span and topic_span.get_text().strip()

                date_spans = find_at_most_one(definition_span, "span", class_="dg")
                date = date_spans.get_text().strip() if date_spans else None

                definitions.append(
                    Definition(
                        pos_modifier=local_pos_modifier or global_pos_modifier,
                        definition=definition,
                        examples=examples,
                        topic=topic,
                        date=date,
                    )
                )
            else:
                xrg = find_exactly_one(entry_span, "span", class_="xrg")
                referenced_term = find_exactly_one(xrg, "span", class_="xr")
                reference = referenced_term.get_text().strip()
                definitions.append(ReferenceDefinition(pos_modifier=global_pos_modifier, reference=reference,))
        return definitions

    @classmethod
    def parse_sense(cls, parsed_entry):
        pos_spans = parsed_entry("span", class_="tg_pos")
        if len(pos_spans) > 1:
            pos = " ".join([e.get_text().strip() for e in pos_spans])
        elif not pos_spans:
            pos_span = find_at_most_one(parsed_entry, "span", class_="posg")
            pos = pos_span.get_text().strip() if pos_span else None
        else:
            pos = pos_spans[0].get_text().strip()

        if parsed_entry.findChildren("span", class_="se2"):
            sense_definitions = []
            for c in parsed_entry.children:
                if set(c["class"]) & set(("tg_pos", "posg", "x_xdh")):
                    continue
                elif not c.get_text().strip():
                    continue
                elif "se2" in c["class"]:
                    sense_definitions.append(cls.parse_sense_definitions(c))
                else:
                    raise InvalidParseAssumptionError(f"WEIRD TAG: {c}")
        else:
            sense_definitions = cls.parse_sense_definitions(parsed_entry)

        if not sense_definitions:
            raise InvalidParseAssumptionError("No sense definitions!")
        return Sense(pos=pos, definitions=sense_definitions)

    @classmethod
    def parse_derivatives(cls, parsed_entry):
        words = find_at_least_one(parsed_entry, "span", class_="l")
        return [e.get_text().strip() for e in words]

    @classmethod
    def parse_origin(cls, parsed_entry):
        etym_type = find_exactly_one(parsed_entry, "span", class_="tg_etym", recursive=False)
        if etym_type.get_text().strip() != "ORIGIN":
            raise InvalidParseAssumptionError(f"Unexpected etym type: {etym_type}")

        origin_span = find_exactly_one(parsed_entry, "span", class_="x_xo1")
        origin = origin_span.get_text().strip()
        return origin

    @classmethod
    def parse(cls, parsed_entry):
        entry = find_exactly_one(parsed_entry, "d:entry")
        head_entry = find_exactly_one(entry, "span", class_="hg")
        defn_entry = find_exactly_one(entry, "span", class_="sg")

        head_word_span = find_exactly_one(head_entry, "span", class_="hw")
        word = " ".join([t.strip() for t in head_word_span.contents if type(t) == bs4.element.NavigableString]).strip()

        variant_span = find_at_most_one(head_word_span, "span", class_="tg_hw")
        variant = int(variant_span.get_text()) if variant_span else None

        pronounciations = cls.parse_pronounciations(head_entry)

        senses = defn_entry("span", class_="se1")
        if len(senses) == 0:
            raise InvalidParseAssumptionError(f"No senses found!")

        senses = []
        for c in defn_entry.children:
            if "se1" in c["class"]:
                senses.append(cls.parse_sense(c))
            elif c.get_text().strip():
                raise InvalidParseAssumptionError(f"Weird tag found in definition: {c.prettify()}!")

        phrases = []
        origin = None
        subentries = entry.find_all("span", class_="t_derivatives")  # derivatives # TODO: verify
        derivatives = []

        for subentry in entry.children:
            if subentry == head_entry or subentry == defn_entry:
                continue
            elif "t_phrases" in subentry["class"]:
                phrases = cls.parse_sense_definitions(subentry)
                continue
            elif "t_derivatives" in subentry["class"]:
                derivatives = cls.parse_derivatives(subentry)
            elif "etym" in subentry["class"]:
                origin = cls.parse_origin(subentry)
                continue
            else:
                raise InvalidParseAssumptionError(f"Weird entry found: {subentry}")

        # TODO: determine other direct children types
        return Entry(
            word=word,
            variant=variant,
            pronounciations=pronounciations,
            senses=senses,
            phrases=phrases,
            origin=origin,
            derivatives=derivatives,
        )


class DictionaryDefinitionDataset(Dataset):
    @classmethod
    def title_tokenization(cls, title):
        return f"<title>{title}</title>"

    @classmethod
    def _make_example(cls, tokenizer, definition):
        max_len = self.max_len

        m = re.match(r"\s*" + re.escape(definition.title) + r"\d*\s*(\|[^|]*\|)?\s*", definition.entry_str,)
        if m:
            trainable_entry = definition.entry_str[m.span()[1] :].strip()
            if not trainable_entry:
                raise RuntimeError(f"Bad entry for {definition.title}: '{definition.entry_str}'")
        else:
            raise RuntimeError(f"Couldn't match {definition.title} on '{definition.entry_str}'")

        tokenized_title = [tokenizer.bos_token_id] + tokenizer.convert_tokens_to_ids(
            tokenizer.tokenize(cls.title_tokenization(definition.title))
        )
        tokenized_entry = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(trainable_entry))

        if len(tokenized_title) + len(tokenized_entry) > max_len:
            logger.warn(f"Truncating long entry for '{definition.title}' (entry is {len(tokenized_entry)})")

        all_tokenized = (tokenized_title + tokenized_entry)[:max_len]
        example = tokenizer.build_inputs_with_special_tokens(all_tokenized)
        assert len(example) == len(all_tokenized), "If this fails our tokenizer is weird"
        bool_mask = [bool(i > 1 and i <= len(tokenized_title)) for i in range(len(example))]

        return (example, bool_mask)

    def __init__(
        self, tokenizer: PreTrainedTokenizer, args, file_path: str, splits=(1.0), split_idx=0,
    ):
        assert os.path.isfile(file_path) or os.path.islink(file_path)

        self.max_len = min(tokenizer.max_len_single_sentence, args.block_size)

        directory, filename = os.path.split(file_path)

        cached_features_file = os.path.join(
            directory,
            args.model_type
            + "_cached_lm_splits_"
            + "_".join(str(e) for e in splits)
            + "_split_idx_"
            + str(split_idx)
            + "_max_len_"
            + str(self.max_len)
            + "_"
            + filename,
        )

        splits_tensor = torch.tensor(splits)
        sum_splits = torch.cumsum(splits_tensor, 0)

        if sum_splits[-1] != 1.0:
            raise RuntimeError(f"Splits must sum to 1 (actual: {sum_splits[-1]})")
        elif split_idx >= len(sum_splits):
            raise RuntimeError(f"Invalid split index {split_idx} (must be less than {len(sum_splits)})")

        if split_idx == 0:
            start_range = 0.0
        else:
            start_range = sum_splits[split_idx - 1]

        end_range = sum_splits[split_idx]

        def in_split(dictionary_definiton):
            val = int(hashlib.md5(dictionary_definition.title.encode("utf-8")).hexdigest(), 16,) % 10000 / 10000
            return (val >= start_range and val < end_range).item()

        if os.path.exists(cached_features_file) and not args.overwrite_cache:
            logger.info("Loading features from cached file %s", cached_features_file)
            with open(cached_features_file, "rb") as handle:
                self.examples = pickle.load(handle)
            logger.info("Loaded {len(self.examples)} features")
        else:
            logger.info("Creating features from dataset file at %s", directory)

            self.examples = []

            with open(file_path, "rb") as f:
                for dictionary_definition in DictionaryDefinition.gen_from_apple_dictionary(f):
                    if in_split(dictionary_definition):
                        self.examples.append(self._make_example(tokenizer, dictionary_definition))

            # Note that we are loosing the last truncated example here for the sake of simplicity (no padding)
            # If your dataset is small, first you should loook for a bigger one :-) and second you
            # can change this behavior by adding (model specific) padding.

            logger.info(f"Saving {len(self.examples)} features into cached file {cached_features_file}")
            with open(cached_features_file, "wb") as handle:
                pickle.dump(self.examples, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, item):
        return (
            torch.tensor(self.examples[item][0], dtype=torch.long),
            torch.tensor(self.examples[item][1], dtype=torch.bool),
        )


def generate_words(
    tokenizer,
    model,
    allow_proper_nouns=True,
    blacklist=(),
    prefix="<title>",
    num=100,
    batch_size=50,
    max_length=400,
    max_iterations=20,
):
    ret = []
    num_iteration = 0

    input = tokenizer.encode(prefix, return_tensors="pt").to("cuda")

    while len(ret) < num and num_iteration < max_iterations:
        num_iteration += 1

        generated = model.generate(input, max_length=max_length, num_return_sequences=batch_size, temperature=1.0,)
        valid_i = 0

        for i in range(generated.size()[0]):
            sentence_tokens = generated[i, :].tolist()
            decoded = tokenizer.decode(sentence_tokens)
            m = re.search(r"<title>(.*?)</title>(.*)", decoded)
            if m:
                title = m.group(1).strip()
                if not allow_proper_nouns and title[:1].upper() == title[:1]:
                    continue
                elif title.upper() in blacklist or title.upper().rstrip("s") in blacklist:
                    continue
                else:
                    ret.append(DictionaryDefinition(title=title, entry_str=m.group(2).rstrip("!")))
            else:
                logger.warning(f'Unable to match regex in "{decoded}"')

    return ret
