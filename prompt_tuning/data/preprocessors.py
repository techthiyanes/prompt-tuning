# Copyright 2022 Google.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Preprocessors used for prompt-tuning.

The main work horse is the `remove_first_text_token` which is used to remove the
initial text token of the input. This lets us get a version of the t5
(Super)?GLUE tasks without having to fork their preprocessors.
"""

from typing import Optional, Sequence
import seqio
import t5.data.preprocessors
import tensorflow.compat.v2 as tf

_string_join = t5.data.preprocessors._string_join  # pylint: disable=protected-access
_pad_punctuation = t5.data.preprocessors._pad_punctuation  # pylint: disable=protected-access

AUTOTUNE = tf.data.experimental.AUTOTUNE


# ========== Text Preprocessors ==========
@seqio.map_over_dataset
def preprocess_tsv_to_qa(line,
                         field_delim='\t',
                         answer_delim='|||',
                         field_names: Optional[Sequence[str]] = None):
  """Preprocessor for turning a TSV into a T5 QA (SQuAD-like) task.

  The input tsv should have the following columns, `id`, `question` `context`
  `answer` (used as the target) and `answers` (multiple correct answers joined
  with the `answer_delim` argument). It will produce a Tensor dict of strings
  in the following format.

  ```
  {
    "inputs": "question: <question_field> context: <context_field>",
    "targets": "<answer_field>",
    "id": "<id_field>",
    "question": "<question_field>",
    "context": "<context_field>",
    "answer": "<answer_field>",
    "answers": [<answers_field>.split(delim)]
  }
  ```

  Args:
    line: A line of text from the dataset that includes a single csv record.
    field_delim: The character the separates columns in the csv.
    answer_delim: The character that separates answers in the `answers` filed
      which contains a serialized list of answers.
    field_names: A list of the column names in the csv.

  Returns:
    A SQuAD like example created from the csv record.
  """
  field_names = ['id', 'context', 'question', 'answer', 'answers'
                ] if field_names is None else field_names

  # Example already has the `id`, `question`, `answer` and `answers` fields.
  example = dict(
      zip(
          field_names,
          tf.io.decode_csv(
              line,
              record_defaults=[''] * len(field_names),
              field_delim=field_delim,
              use_quote_delim=False)))
  example['question'] = _pad_punctuation(example['question'])
  example['context'] = _pad_punctuation(example['context'])
  example['answer'] = _pad_punctuation(example['answer'])
  example['inputs'] = _string_join(
      ['question:', example['question'], 'context:', example['context']])
  example['targets'] = example['answer']
  example['answers'] = _pad_punctuation(
      tf.strings.split([example['answers']], answer_delim).values)
  return example


@seqio.map_over_dataset
def preprocess_text_generation(
    example,
    source_key,
    target_key,
    task_name=None,
    prefix=None,
    source_nested_key=None,
    target_nested_key=None,
):
  """Convert a text generation dataset to a text-to-text format.

  Each {<source_text>, <target_text>} example will have the format:
  {'inputs': <task_name> <prefix>: <source_text>, 'targets': <target_text>}

  Args:
    example: An example to process.
    source_key: The key for the source text.
    target_key: The key for the target text.
    task_name: The name of the task.
    prefix: A text that specifies how the model should perform the task.
    source_nested_key: The nested key for the source text (if any).
    target_nested_key: The nested key for the target text (if any).

  Returns:
    A preprocessed example with the format listed above.
  """

  source_text = example[source_key] if source_nested_key is None else example[
      source_key][source_nested_key]
  target_text = example[target_key] if target_nested_key is None else example[
      target_key][target_nested_key]

  strs_to_join = [s for s in [task_name, prefix, source_text] if s is not None]

  return {
      'inputs': tf.strings.join(strs_to_join, separator=' '),
      'targets': target_text
  }


@seqio.map_over_dataset
def remove_first_text_token(example, key: str = 'inputs'):
  # Autograph doesn't allow split->slice[1:]->join to remove the first
  # token of text, so do it with a regex, anchor to front and match all
  # non-space characters until (and including) first space, replace with an
  # empty string.
  example[key] = tf.strings.regex_replace(
      example[key], r'^[^ ]* ', '', replace_global=False)
  # example[key] = _string_join(tf.strings.split(example[key]))[1:])
  return example


# ========== Token Preprocessors ==========
def add_sentinel_to_targets(ds, output_features):
  return add_sentinel_to_beginning(
      ds, output_features, field='targets', offset=0)


def add_sentinel_to_beginning(ds,
                              output_features,
                              field: str = 'targets',
                              offset: int = 0):
  r"""Add <extra_id_\d> to the beginning of each target.

  Note:
    Should be used as a token preprocessor.

  Args:
    ds: The dataset we are applying this function to.
    output_features: The output features for our task.
    field: Which field in the dict should we add the token to?
    offset: Which of the <extra_id_\d> tokens do you want?

  Returns:
    A dataset where the <extra_id_\d> has been appended to each target (in the
    token space, not the raw text).
  """
  vocab = output_features[field].vocabulary
  # <extra_id_0> is always the last item in the vocab.
  value_to_add = vocab.vocab_size - (offset + 1)

  @seqio.map_over_dataset
  def add(ex):
    ex[field] = tf.concat([tf.expand_dims(value_to_add, axis=0), ex[field]],
                          axis=0)
    return ex

  return add(ds)


def build_langid_filter(lang_code, lang_detector, threshold):
  """Create a langid filter function of the lang_code at given threshold.

  This filtering matches the mC4 LangID, and makes the threshold configurable.
  mC4 uses 0.7 as threshold.

  Args:
    lang_code: The language code we are considering
    lang_detector: The langID detector
    threshold: a float, the threshold to filter langid probability

  Returns:
    a langid filter function.
  """

  def filter_fn(text):
    """Python function to filter texts based on lang id score."""
    result = lang_detector.find_language(text=text)
    return (result.is_reliable and result.probability >= threshold and
            result.language == lang_code)

  return filter_fn


def filter_langid(dataset,
                  lang_code,
                  lang_detector,
                  text_key='text',
                  threshold=0.95):
  """Create a dataset with langid confidence more than the given threshold.

  The input examples should have a key text_key associated with a tf.string
  value.

  Args:
    dataset: a tf.data.Dataset
    lang_code: The language code we are considering
    lang_detector: The langID detector
    text_key: a string, the key for the text feature to preprocess in the
      dataset examples.
    threshold: a float, the threshold to filter langid probability.

  Returns:
    a tf.data.Dataset
  """
  filter_fn = build_langid_filter(lang_code, lang_detector, threshold)

  dataset = dataset.filter(
      lambda x: tf.numpy_function(filter_fn, [x[text_key]], tf.bool))
  return dataset
