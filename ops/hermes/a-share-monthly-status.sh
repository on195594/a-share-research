#!/usr/bin/env bash
# Monthly batch completion check; see a-share-monthly.sh.
exec bash "$(dirname "$0")/a-share-monthly.sh" status
