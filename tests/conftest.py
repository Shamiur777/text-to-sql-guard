import pytest

from text_to_sql.db import build_sample_db
from text_to_sql.executor import SafeExecutor, open_readonly
from text_to_sql.policy import Catalog, Policy


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    return build_sample_db(tmp_path_factory.mktemp("db") / "company.db")


@pytest.fixture(scope="session")
def policy():
    return Policy()


@pytest.fixture(scope="session")
def catalog(db_path, policy):
    con = open_readonly(db_path)
    cat = Catalog.from_connection(con, policy)
    con.close()
    return cat


@pytest.fixture()
def executor(db_path, catalog):
    ex = SafeExecutor(db_path, catalog, Policy(timeout_s=0.3))
    yield ex
    ex.close()
