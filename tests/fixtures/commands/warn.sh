#!/usr/bin/env bash
# summary: fixture, reports one WARN
vws_cmd_main() {
  vws_emit OK first "fine"
  vws_emit WARN second "careful"
}
