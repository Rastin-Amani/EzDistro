import os
from pocketbase import PocketBase

# Fetches PB_URL from the environment, defaults to local if not set
PB_URL = os.getenv("PB_URL", "http://db.dev.seoz.cloud")
pb = PocketBase(PB_URL)

def get_pb():
    return PocketBase(PB_URL)