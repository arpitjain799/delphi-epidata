from flask import Blueprint, request

from .._config import AUTH
from .._params import extract_integers, extract_strings
from .._query import execute_query, QueryBuilder
from .._validate import check_auth_token, require_all

# first argument is the endpoint name
bp = Blueprint("quidel", __name__)
alias = None


@bp.route("/", methods=("GET", "POST"))
def handle():
    check_auth_token(request, AUTH["quidel"])
    require_all(request, "locations", "epiweeks")

    locations = extract_strings("locations")
    epiweeks = extract_integers("epiweeks")

    # build query
    q = QueryBuilder("quidel", "q")

    fields_string = ["location"]
    fields_int = ["epiweek"]
    fields_float = ["value"]
    q.set_fields(fields_string, fields_int, fields_float)

    q.set_sort_order("epiweek", "location")

    # build the filter
    q.where_strings("location", locations)
    q.where_integers("epiweek", epiweeks)

    # send query
    return execute_query(str(q), q.params, fields_string, fields_int, fields_float)
