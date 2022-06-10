#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#
import json
from datetime import datetime
from time import sleep
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple, Union

import requests
from airbyte_cdk import AirbyteLogger
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http import HttpStream
from airbyte_cdk.sources.streams.http.auth import NoAuth
from google_play_scraper import Sort as GooglePlaySort
from google_play_scraper.constants.element import ElementSpecs as GooglePlayElementSpecs
from google_play_scraper.constants.regex import Regex as GooglePlayRegex
from google_play_scraper.constants.request import Formats as GooglePlayFormats

URL_BASE = "https://play.google.com"


class Reviews(HttpStream):
    url_base = URL_BASE
    cursor_field = "ds"
    primary_key = "ds"

    @staticmethod
    def __datetime_to_str(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def __str_to_datetime(d: str) -> datetime:
        return datetime.strptime(d, "%Y-%m-%dT%H:%M:%S")

    # noinspection PyUnusedLocal
    def __init__(self, config, **kwargs):
        super().__init__()
        self.__config = config

        self.__logger = AirbyteLogger()

        self.__language_ind = 0
        self.__country_ind = 0
        self.__count_in_req = 0
        self.__count = 0
        self.__total_count = 0

        self.__cursor_value = self.__str_to_datetime(self.__config["start_time"])
        self.__tmp_cursor_value = self.__str_to_datetime(self.__config["start_time"])

        self.__logger.info("Read latest review timestamp from config: {}".format(self.__config["start_time"]))

    @property
    def state(self) -> Mapping[str, Any]:
        ds = self.__datetime_to_str(self.__tmp_cursor_value)
        self.__logger.info("Saved latest review timestamp to file: {}".format(ds))
        return {self.cursor_field: ds}

    @state.setter
    def state(self, value: Mapping[str, Any]):
        ds = value.get(self.cursor_field)
        if ds is not None:
            self.__logger.info("Read latest review timestamp from file: {}".format(ds))
            self.__cursor_value = self.__str_to_datetime(ds)
            self.__tmp_cursor_value = self.__str_to_datetime(ds)

    def read_records(self, *args, **kwargs) -> Iterable[Mapping[str, Any]]:
        for record in super().read_records(*args, **kwargs):
            ds = record.get("ds")
            if ds is not None:
                self.__tmp_cursor_value = max(self.__tmp_cursor_value, ds)
            yield record

    http_method = "POST"

    def path(
            self,
            *,
            stream_state: Mapping[str, Any] = None,
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> str:
        return "/_/PlayStoreUi/data/batchexecute"

    def request_params(
            self,
            stream_state: Mapping[str, Any],
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        return {
            "hl": self.__config["languages"][self.__language_ind],
            "gl": self.__config["countries"][self.__country_ind]
        }

    def request_headers(
            self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> Mapping[str, Any]:
        return {"content-type": "application/x-www-form-urlencoded"}

    def request_body_data(
            self,
            stream_state: Mapping[str, Any],
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> Optional[Union[Mapping, str]]:
        count = self.__config.get("max_reviews_per_req")
        if count is None:
            count = 100
        if (next_page_token is None) or isinstance(next_page_token, list):
            # noinspection PyProtectedMember
            res = GooglePlayFormats._Reviews.PAYLOAD_FORMAT_FOR_FIRST_PAGE.format(
                app_id=self.__config["app_id"], sort=GooglePlaySort.NEWEST, count=count, score="null"
            )
        else:
            # noinspection PyProtectedMember
            res = GooglePlayFormats._Reviews.PAYLOAD_FORMAT_FOR_PAGINATED_PAGE.format(
                app_id=self.__config["app_id"], sort=GooglePlaySort.NEWEST, count=count, score="null", pagination_token=next_page_token
            )
        return res

    @staticmethod
    def __fetch_review_items(response: requests.Response):
        dom = response.text
        match = json.loads(GooglePlayRegex.REVIEWS.findall(dom)[0])
        return json.loads(match[0][2])[0]

    @staticmethod
    def __rename_field(v: dict, v_name: str, v1: dict, v1_name: str):
        field = v.get(v_name)
        if field is not None:
            v1[v1_name] = field

    def __transform(self, v: dict):
        v1 = {
            "source": "Google Play",
            "lang": self.__config["languages"][self.__language_ind],
            "country": self.__config["countries"][self.__country_ind]
        }

        self.__rename_field(v, "reviewId", v1, "ticket_id")
        self.__rename_field(v, "userName", v1, "user_id")
        self.__rename_field(v, "content", v1, "message")
        self.__rename_field(v, "score", v1, "score")
        self.__rename_field(v, "at", v1, "ds")
        self.__rename_field(v, "reviewCreatedVersion", v1, "version")

        return v1

    def parse_response(
            self,
            response: requests.Response,
            *,
            stream_state: Mapping[str, Any],
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        result = []
        review_items = self.__fetch_review_items(response)
        for review in review_items:
            v = {
                k: spec.extract_content(review)
                for k, spec in GooglePlayElementSpecs.Review.items()
            }
            v = self.__transform(v)
            ds = v.get("ds")
            if (ds is None) or (ds > self.__cursor_value):
                result.append(v)
        self.__count_in_req = len(result)
        self.__count += self.__count_in_req
        return result

    def __fetch_next_page_token(self, response: requests.Response):
        dom = response.text
        match = json.loads(GooglePlayRegex.REVIEWS.findall(dom)[0])
        token = json.loads(match[0][2])[-1][-1]
        if (self.__count_in_req == 0) or isinstance(token, list):
            self.__logger.info("Fetched {} reviews for country=\"{}\" and language=\"{}\"".format(
                self.__count, self.__config["countries"][self.__country_ind], self.__config["languages"][self.__language_ind]
            ))
            self.__language_ind += 1
            if self.__language_ind == len(self.__config["languages"]):
                self.__country_ind += 1
                self.__language_ind = 0
            if self.__country_ind == len(self.__config["countries"]):
                self.__logger.info("Totally fetched {} reviews".format(self.__total_count + self.__count))
                token = None
            self.__total_count += self.__count
            self.__count = 0
        return token

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        timeout_ms = self.__config.get("timeout_ms")
        if timeout_ms is not None:
            sleep(timeout_ms / 1000.0)
        return self.__fetch_next_page_token(response)


class SourceGooglePlay(AbstractSource):
    @staticmethod
    def __get_matches(path: str, params: Mapping[str, str]):
        response = requests.get(URL_BASE + path, params=params)
        dom = response.text
        matches = GooglePlayRegex.SCRIPT.findall(dom)
        return matches

    def check_connection(self, logger, config) -> Tuple[bool, any]:
        logger.info("Checking connection configuration for app \"\"...".format(config["app_id"]))

        path = "/store/apps/details"
        params = {
            "id": config["app_id"]
        }

        logger.info("Checking \"app_id\"...")
        matches = self.__get_matches(path, params)
        if len(matches) == 0:
            error_text = "\"app_id\" \"{}\" is invalid!".format(config["app_id"])
            logger.error(error_text)
            return False, {"key": "app_id", "value": config["app_id"], "error_text": error_text}
        logger.info("\"app_id\" \"{}\" is valid".format(config["app_id"]))

        logger.info("Checking \"countries\" values...")
        for ind, gl in enumerate(config["countries"]):
            logger.info("Checking \"countries\" {}'th value \"{}\"".format(ind, gl))
            params["gl"] = gl
            response = requests.get(URL_BASE + path, params=params)
            dom = response.text
            matches = GooglePlayRegex.SCRIPT.findall(dom)
            if len(matches) == 0:
                error_text = "\"countries\" {}'th value \"{}\" is invalid!".format(ind, gl)
                logger.error(error_text)
                return False, {"key": "countries", "value": config["countries"], "error_text": error_text}
            logger.info("\"countries\" {}'th value \"{}\" is valid".format(ind, gl))
        logger.info("\"countries\" values are valid")

        logger.info("Connection configuration is valid!")
        return True, None

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        auth = NoAuth()
        return [Reviews(authenticator=auth, config=config)]
