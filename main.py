#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#


import sys

from airbyte_cdk.entrypoint import launch
from source_google_play import SourceGooglePlay

if __name__ == "__main__":
    source = SourceGooglePlay()
    launch(source, sys.argv[1:])
