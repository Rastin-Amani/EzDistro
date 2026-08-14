import os
from fastapi.templating import Jinja2Templates
import jdatetime
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# Define the filter
def to_jalali_year(date_str):
    if not date_str:
        return ""
    try:
        # 1. Clean the 'Z' off the end
        clean_date = str(date_str).replace("Z", "").strip()

        # 2. Dynamically check for the decimal point
        if "." in clean_date:
            gregorian_dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S.%f")
        else:
            gregorian_dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S")

        jalali_dt = jdatetime.datetime.fromgregorian(datetime=gregorian_dt)
        return jalali_dt.year
    except (ValueError, TypeError) as e:
        from structlog import get_logger

        logger = get_logger(__name__)
        logger.warning("date_parse_error.year", error=str(e), raw_string=date_str)
        return date_str


def to_jalali_date(date_str):
    if not date_str:
        return ""
    try:
        # 1. Clean the 'Z' off the end
        clean_date = str(date_str).replace("Z", "").strip()

        # 2. Dynamically check for the decimal point
        if "." in clean_date:
            gregorian_dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S.%f")
        else:
            gregorian_dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S")

        jalali_dt = jdatetime.datetime.fromgregorian(datetime=gregorian_dt)
        return jalali_dt.strftime("%Y/%m/%d")
    except (ValueError, TypeError) as e:
        from structlog import get_logger

        logger = get_logger(__name__)
        logger.warning("date_parse_error.date", error=str(e), raw_string=date_str)
        return date_str


# Register the filters
templates.env.filters["jalali_year"] = to_jalali_year
templates.env.filters["jalali_date"] = to_jalali_date
