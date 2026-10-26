# Lint fixture (not imported): an orchestrate-like module. Lines with "writes" are the functions the
# restart-test lint must find.
def one_write(gh, path):
    gh.write("PATCH", path, {"body": "x"})


def two_sites(gh, base):  # writes
    gh.write("PATCH", base, {"state": "closed", "state_reason": "completed"})
    gh.write("POST", f"{base}/comments", {"body": "done"})


def in_a_loop(client, calls):  # writes
    for method, path, payload in calls:
        client.write(method, path, payload)


def batched_graphql(gh, batches):  # writes
    return [gh.graphql("mutation{}", {}, write=True) for _ in batches]


def graphql_read(gh):
    gh.graphql("query{}", {})
    gh.graphql("query{}", {}, write=False)


def file_writes(f, lines):
    for line in lines:
        f.write(line + "\n")
