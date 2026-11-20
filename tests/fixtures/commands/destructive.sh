#!/usr/bin/env bash
# summary: fixture, reports a deletion and needs --yes
vws_cmd_main() {
  vws_confirm "deleting the item"
  vws_emit OK delete "deleted"
}
