"""
.. _xlmr_classifier:

This module provides a trainable XLMR classifier.
[read-more](https://arxiv.org/abs/1911.02116)
"""
import importlib
import os
import pickle
import random
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sklearn import preprocessing
from tqdm import tqdm

import dialogy.constants as const
from dialogy.base import Guard, Input, Output, Plugin
from dialogy.types import Intent
from dialogy.utils import load_file, logger, read_from_json, save_file


class XLMRMultiClass(Plugin):
    """
    This plugin provides a classifier based on `XLM-Roberta <https://arxiv.org/abs/1911.02116>`.

    .. _XLMRMultiClass:
    The use_state flag in the XLMRMultiClass plugin is used to enable the use of state variable as the part of the text input.
    """

    def __init__(
        self,
        model_dir: str,
        dest: Optional[str] = None,
        guards: Optional[List[Guard]] = None,
        debug: bool = False,
        threshold: float = 0.1,
        use_cuda: bool = False,
        score_round_off: int = 5,
        purpose: str = const.TRAIN,
        fallback_label: str = const.ERROR_LABEL,
        use_state: bool = False,
        data_column: str = const.DATA,
        label_column: str = const.LABELS,
        state_column: str = const.STATE,
        lang_column: str = const.LANG,
        args_map: Optional[Dict[str, Any]] = None,
        skip_labels: Optional[List[str]] = None,
        prompts_map: dict = {},
        use_prompt: bool = False,
        train_using_all_prompts: bool = True,
        null_prompt_token: str = const.NULL_PROMPT_TOKEN,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            classifer = getattr(
                importlib.import_module(const.XLMR_MODULE), const.XLMR_MULTI_CLASS_MODEL
            )
        except ModuleNotFoundError as error:
            raise ModuleNotFoundError(
                "Plugin requires simpletransformers -- https://simpletransformers.ai/docs/installation/"
            ) from error

        super().__init__(dest=dest, guards=guards, debug=debug)
        self.labelencoder = preprocessing.LabelEncoder()
        self.classifier = classifer
        self.model: Any = None
        self.model_dir = model_dir
        self.fallback_label = fallback_label
        self.data_column = data_column
        self.label_column = label_column
        self.state_column = state_column
        self.lang_column = lang_column
        self.use_cuda = use_cuda
        self.use_state = use_state
        self.labelencoder_file_path = os.path.join(
            self.model_dir, const.LABELENCODER_FILE
        )
        self.ts_parameter: float = (
            read_from_json(
                [const.TS_PARAMETER], model_dir, const.CALIBRATION_CONFIG_FILE
            ).get(const.TS_PARAMETER)
            or 1.0
        )

        self.threshold = threshold
        self.skip_labels = set(skip_labels or set())
        self.purpose = purpose
        self.round = score_round_off
        if args_map and (
            const.TRAIN not in args_map
            or const.TEST not in args_map
            or const.PRODUCTION not in args_map
        ):
            raise ValueError(
                f"Attempting to set invalid {args_map}. "
                f"It is missing some of {const.TRAIN}, {const.TEST}, {const.PRODUCTION} in configs."
            )
        self.args_map = args_map
        self.kwargs = kwargs or {}
        try:
            if os.path.exists(self.labelencoder_file_path):
                self.init_model()
        except EOFError:
            logger.error(
                f"Plugin {self} Failed to load labelencoder from {self.labelencoder_file_path}. "
                "Ignore this message if you are training but if you are using this in production or testing, then this is serious!"
            )
        self.prompts_map = prompts_map
        self.use_prompt = use_prompt
        self.train_using_all_prompts = train_using_all_prompts
        self.null_prompt_token = null_prompt_token
        self.debug = debug

    def init_model(self, label_count: Optional[int] = None) -> None:
        """
        Initialize the model if artifacts are available.

        :param label_count: number of labels to train on or predict, defaults to None
        :type label_count: Optional[int], optional
        :raises ValueError: In case n is not provided or can't be calculated.
        """
        model_dir = const.XLMR_MODEL_TIER
        if os.path.exists(self.labelencoder_file_path):
            self.load()
            label_count = len(self.labelencoder.classes_)
            model_dir = self.model_dir
        if not label_count:
            raise ValueError(
                f"Plugin {self} needs either the training data "
                "or an existing labelencoder to initialize."
            )
        args = (
            self.args_map[self.purpose]
            if self.args_map and self.purpose in self.args_map
            else {}
        )
        self.use_calibration = args.get(const.MODEL_CALIBRATION)
        try:
            self.model = self.classifier(
                const.XLMR_MODEL,
                model_dir,
                num_labels=label_count,
                use_cuda=self.use_cuda,
                args=args,
                **self.kwargs,
            )
        except OSError:
            self.model = self.classifier(
                const.XLMR_MODEL,
                const.XLMR_MODEL_TIER,
                num_labels=label_count,
                use_cuda=self.use_cuda,
                args=args,
                **self.kwargs,
            )

    @property
    def valid_labelencoder(self) -> bool:
        return hasattr(self.labelencoder, "classes_")

    def inference(
        self, texts: Optional[List[str]], state: Optional[str] = None, lang: Optional[str] = None,
    ) -> List[Intent]:
        
        """
        Predict the intent of a list of texts.
        If the model has been trained using the state features, it expects the text to also be appended with the state token else the predictions would be spurious.

        :param texts: A list of strings, derived from ASR transcripts.
        :param state: state, mapped to the ASR transcripts.
        :type texts: List[str]
        :type state: List[str]
        :raises AttributeError: In case the labelencoder is not available.
        :return: A list of intents corresponding to texts.
        :rtype: List[Intent]
        """
        logger.debug(f"Classifier Input:\n{texts}")
        
        fallback_output = Intent(name=self.fallback_label, score=1.0).add_parser(self)

        if not texts:
            logger.error(f"texts passed to model {texts}!")
            return [fallback_output]

        if self.model is None:
            logger.error(f"No model found for plugin {self.__class__.__name__}!")
            return [fallback_output]

        if self.use_state and not state:
            raise ValueError(
                f"Plugin {self.__class__.__name__} requires state to be passed to the model."
            )

        if self.use_prompt and not state:
            raise ValueError(
                f"In order to use prompts as feature, Plugin {self.__class__.__name__} is requires state to be passed to the model."
            )

        if self.use_prompt and not lang:
            raise ValueError(
                f"In order to use prompts as feature, Plugin {self.__class__.__name__} is requires lang to be passed to the model."
            )

        if self.use_prompt and state:
            texts[0] += "<s> " + self.lookup_prompt(lang, state)[0] + " </s>"
            if self.debug:
                logger.debug(f"Classification input (Prompt as a feature enabled):\n{texts}")
                logger.debug(texts)
                # logger.debug(self.lookup_prompt(lang, state)[0])

        if self.use_state and state:
            texts[0] += "<s> " + state + " </s>"
            if self.debug:
                logger.debug(texts)
                logger.debug(f"Classifition input (State as a feature enabled):\n{texts}")

        if not self.valid_labelencoder:
            raise AttributeError(
                "Seems like you forgot to "
                f"save the {self.__class__.__name__} plugin."
            )
            
        predictions, logits = self.model.predict(texts)
        if not predictions:
            return [fallback_output]

        logits = logits / self.ts_parameter
        confidence_scores = [np.exp(logit) / sum(np.exp(logit)) for logit in logits]
        intents_confidence_order = np.argsort(confidence_scores)[0][::-1]
        predicted_intents = self.labelencoder.inverse_transform(
            intents_confidence_order
        )
        ordered_confidence_scores = [
            confidence_scores[0][idx] for idx in intents_confidence_order
        ]

        if self.use_calibration:
            ordered_confidence_scores = [
                logits[0][idx] for idx in np.argsort(logits)[0][::-1]
            ]  # ordered logits for calibration

        return [
            Intent(name=intent, score=round(score, self.round)).add_parser(self)
            for intent, score in zip(predicted_intents, ordered_confidence_scores)
        ]

    def validate(self, training_data: pd.DataFrame) -> bool:
        """
        Validate the training data is in the appropriate format

        :param training_data: A pandas dataframe containing at least list of strings and corresponding labels.
            Should also contain a state key if use_state = True while initializing object.
        :type training_data: pd.DataFrame
        :return: True if the dataframe is valid, False otherwise.
        :rtype: bool
        """
        if training_data.empty:
            logger.error("Training dataframe is empty.")
            return False
        expected_columns = [self.data_column, self.label_column]

        if self.use_prompt:
            expected_columns.append(self.lang_column)
            expected_columns.append(self.state_column)

        if self.use_state:
            if self.state_column not in expected_columns:
                expected_columns.append(self.state_column)
        
        for column in expected_columns:
            if column not in training_data.columns:
                logger.warning(f"Column {column} not found in training data")
                return False
        return True

    def train(self, training_data: pd.DataFrame) -> None:
        """
        Train an intent-classifier on the provided training data.

        The training is skipped if the data-format is not valid.
        While training with the use_state flag as true, make sure that the state column is the part of the training_data dataframe
        :param training_data: A pandas dataframe containing at least list of strings and corresponding labels.
        :type training_data: pd.DataFrame
        """
        if not self.validate(training_data):
            logger.warning(
                f"Training dataframe is invalid, for {self.__class__.__name__} plugin."
            )
            return

        skip_labels_filter = training_data[self.label_column].isin(self.skip_labels)
        training_data = training_data[~skip_labels_filter].copy()

        encoder = self.labelencoder.fit(training_data[self.label_column])

        sample_size = 5 if len(training_data) > 5 else len(training_data)
        training_data.rename(
            columns={self.data_column: const.TEXT, self.label_column: const.LABELS},
            inplace=True,
        )
        training_data.loc[:, const.LABELS] = encoder.transform(
            training_data[const.LABELS]
        )

        # Append state to text
        if self.use_state:
            # logger.debug(training_data[const.STATE])
            training_data[const.TEXT] += "<s> " + training_data[self.state_column] + " </s>"

        # Append prompt to text
        if self.use_prompt:
            logger.debug("Adding prompts to input text")

            if not self.train_using_all_prompts:
                for i in tqdm(range(training_data.shape[0]), desc="progress bar:"):
                    _lang = training_data.iloc[i][self.lang_column]
                    _state = training_data.iloc[i][self.state_column]
                    _prompt =  self.lookup_prompt(_lang, _state)[0]
                    training_data.at[i, const.TEXT] = training_data.iloc[i][const.TEXT] \
                            + "<s> " \
                            + _prompt \
                            + " </s>"
            else:
                training_data_ = pd.DataFrame(columns=[const.TEXT, const.LABELS])
                for i in tqdm(range(training_data.shape[0]), desc="progress bar:"):
                    _lang = training_data.iloc[i][self.lang_column]
                    _state = training_data.iloc[i][self.state_column]
                    _prompts =  self.lookup_prompt(_lang, _state, return_all=True)
                    for prompt in _prompts:
                        _row = {
                            const.TEXT : [
                            training_data.iloc[i][const.TEXT] \
                            + "<s> " \
                            + prompt \
                            + " </s>"
                            ],
                            const.LABELS: [training_data.iloc[i][const.LABELS]]
                        }
                        training_data_ = pd.concat([training_data_, pd.DataFrame(_row)], ignore_index=True)
                        # training_data_ = training_data_.append(_row, ignore_index=True)
                        training_data_.reset_index(drop=True)
            
                logger.debug(f"Training dataset size (original): {training_data.shape}")
                logger.debug(f"Training dataset size (after adding prompts): {training_data_.shape}")

                training_data = training_data_
                del training_data_

        training_data.to_csv("/tmp/xlmr-data-train.csv",index=False)                    
        training_data = training_data[[const.TEXT, const.LABELS]]
        self.init_model(len(encoder.classes_))
        logger.debug(
            f"Displaying a few samples (this goes into the model):\n{training_data.sample(sample_size)}\nLabels: {len(encoder.classes_)}."
        )

        self.model.train_model(training_data)
        self.save()

    def save(self) -> None:
        """
        Save the plugin artifacts.

        :raises ValueError: In case the labelencoder is not trained.
        """
        if not self.model or not self.valid_labelencoder:
            raise ValueError(
                f"Plugin {self.__class__.__name__} seems to be un-trained."
            )
        save_file(
            self.labelencoder_file_path,
            self.labelencoder,
            mode="wb",
            writer=pickle.dump,
        )

    def lookup_prompt(self, lang: str, state: str, return_all: bool = False) -> List[str]:
        """
        Same as get_prompt() method, but built for faster lookup to reduce latency during inference. 
        """
        try:
            return random.sample(self.prompts_map.get(lang).get(state), 1)
        except Exception as e:
            if self.debug:
                logger.debug(e)
                logger.debug(f"Prompt not found for Lang: {lang} \t State: {state}")
            return [self.null_prompt_token]
            
    def load(self) -> None:
        """
        Load the plugin artifacts.
        """
        self.labelencoder = load_file(
            self.labelencoder_file_path, mode="rb", loader=pickle.load
        )

    def utility(self, input: Input, _: Output) -> List[Intent]:
        return ( 
        self.inference(input.clf_feature, input.current_state, input.lang)
        )