#!/usr/bin/env bash
set -euo pipefail

slack() {
  local method=$1 response
  shift
  response=$(curl -sS --fail-with-body -H "Authorization: Bearer $SLACK_BOT_TOKEN" "$@" "https://slack.com/api/$method")
  if [ "$(jq -r .ok <<< "$response")" != true ]; then
    echo "::error::Slack $method failed: $(jq -c '{error, warning, response_metadata}' <<< "$response")"
    return 1
  fi
  printf '%s\n' "$response"
}

slack_post() {
  slack "$1" -X POST -H 'Content-Type: application/json; charset=utf-8' --data "$2"
}

# shellcheck disable=SC2016
pr=$(gh api graphql -F owner="${GITHUB_REPOSITORY%/*}" -F name="${GITHUB_REPOSITORY#*/}" -F number="$NUMBER" -f query='
  query($owner: String!, $name: String!, $number: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $number) {
        number title url state isDraft createdAt authorAssociation reviewDecision
        additions deletions changedFiles
        author { __typename login url }
        reviewRequests(first: 20) {
          nodes { requestedReviewer { __typename ... on User { login } ... on Bot { login } ... on Team { slug } } }
        }
        latestOpinionatedReviews(first: 20) { nodes { state author { login } } }
      }
    }
  }' --jq .data.repository.pullRequest)

author=$(jq -r '.author.login // "ghost"' <<< "$pr")
if [ "$(jq -r '.author.__typename // ""' <<< "$pr")" = Bot ]; then
  who=bot
else
  # Team means write access: `authorAssociation` reports a private org member as CONTRIBUTOR.
  permission=$(gh api "repos/$GITHUB_REPOSITORY/collaborators/$author/permission" --jq .permission)
  case "$permission" in admin | write | maintain) who=team ;; *) who=external ;; esac
fi

reviewers=$(jq -r '[.reviewRequests.nodes[].requestedReviewer | select(. != null)
  | if .__typename == "Team" then "@" + .slug else .login end] | join(", ")' <<< "$pr")

oldest=$(jq -r '.createdAt | fromdateiso8601 - 60' <<< "$pr")
ts='' cursor=''
while :; do
  page=$(slack conversations.history -G --data-urlencode "channel=$SLACK_CHANNEL" \
    --data-urlencode "oldest=$oldest" --data-urlencode limit=200 \
    --data-urlencode include_all_metadata=true --data-urlencode "cursor=$cursor")
  ts=$(jq -r --arg repo "$GITHUB_REPOSITORY" --argjson number "$NUMBER" '
    [.messages[] | select(.metadata.event_type == "github_pull_request"
      and .metadata.event_payload.repo == $repo and .metadata.event_payload.number == $number)]
    | last | .ts // empty' <<< "$page")
  cursor=$(jq -r '.response_metadata.next_cursor // ""' <<< "$page")
  if [ -n "$ts" ] || [ -z "$cursor" ]; then break; fi
done

if [ -z "$ts" ]; then
  case "$EVENT/$ACTION" in
    pull_request_target/review_requested) ;;
    pull_request_target/opened | pull_request_target/reopened)
      if [ "$who" = team ]; then
        echo "A team member's pull request waits for its author to request a review"
        exit 0
      fi
      ;;
    *)
      echo "No Slack message for #$NUMBER yet; nothing to update"
      exit 0
      ;;
  esac
fi

text=$(jq -r --arg who "$who" --arg repo "$GITHUB_REPOSITORY" --arg reviewers "$reviewers" '
  def esc: gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;");
  ($reviewers | split(", ")) as $pending
  | [.latestOpinionatedReviews.nodes[] | select(.author != null and (.author.login | IN($pending[]) | not))]
    as $reviews
  | ([$reviews[] | select(.state == "APPROVED") | .author.login] | join(", ")) as $approved
  | ([$reviews[] | select(.state == "CHANGES_REQUESTED") | .author.login] | join(", ")) as $changes
  | (.authorAssociation | IN("FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER")) as $first
  | {
      team: "",
      bot: ":robot_face: *Bot*",
      external: ":warning: *External contributor\(if $first then ", first PR" else "" end)*"
    }[$who] as $tag
  | (if .state == "MERGED" then ":white_check_mark: *Merged*"
     elif .state == "CLOSED" then ":no_entry_sign: *Closed*"
     elif .isDraft then ":construction: *Draft*"
     elif .reviewDecision == "CHANGES_REQUESTED" then ":memo: *Changes requested*"
     elif .reviewDecision == "APPROVED" then ":thumbsup: *Approved*"
     elif $reviewers != "" then ":eyes: *Review requested*"
     elif $tag != "" then $tag
     else ":hourglass_flowing_sand: *Awaiting review*" end) as $status
  | $status + " · <\(.url)|#\(.number) \(.title | esc)>\n"
    + "by <\(.author.url // "https://github.com/ghost")|\(.author.login // "ghost")>"
    + (if $tag != "" and $status != $tag then " (\($tag))" else "" end)
    + (if .state == "OPEN" and $reviewers != "" then " → \($reviewers)" else "" end)
    + (if $approved != "" then " · approved by \($approved)" else "" end)
    + (if $changes != "" then " · changes requested by \($changes)" else "" end)
    + " · \($repo) · +\(.additions) -\(.deletions) in \(.changedFiles) file\(if .changedFiles == 1 then "" else "s" end)"
' <<< "$pr")

message=$(jq -n --arg channel "$SLACK_CHANNEL" --arg text "$text" --arg repo "$GITHUB_REPOSITORY" \
  --argjson number "$NUMBER" '{
    channel: $channel, text: $text, unfurl_links: false,
    metadata: {event_type: "github_pull_request", event_payload: {repo: $repo, number: $number}}
  }')

if [ -z "$ts" ]; then
  slack_post chat.postMessage "$message" > /dev/null
  echo "Posted the Slack message for #$NUMBER"
  exit 0
fi

slack_post chat.update "$(jq --arg ts "$ts" '. + {ts: $ts}' <<< "$message")" > /dev/null
echo "Updated the Slack message for #$NUMBER"

if [ "$EVENT/$ACTION" = pull_request_target/review_requested ] && [ -n "$reviewers" ]; then
  slack_post chat.postMessage "$(jq -n --arg channel "$SLACK_CHANNEL" --arg ts "$ts" \
    --arg text ":eyes: Review requested again → $reviewers" \
    '{channel: $channel, thread_ts: $ts, text: $text}')" > /dev/null
  echo "Added a thread reply for the repeated review request"
fi
