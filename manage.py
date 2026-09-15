#!/usr/bin/env python
"""Django's tooling entry point. Day to day, use `dbrel serve` instead."""

import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
