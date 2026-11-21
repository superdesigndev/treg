# Search podcast transcripts with Exa

Use the existing `exa.web.search` tool. Exa's [podcast library](https://exa.ai/library/podcast)
is part of its web search index, and `includeDomains` limits a search to that library.

```bash
treg call exa.web.search --method POST --data '{
  "query": "podcast episodes where founders discuss pricing mistakes",
  "includeDomains": ["exa.ai/library/podcast"],
  "numResults": 2,
  "contents": {"text": true}
}'
```

`contents.text` requests transcript text in each result. It may include speaker labels and
timestamps. A content limit such as `contents.text.maxCharacters` can truncate the returned text.
Leave out `includeDomains` to search the wider web and podcast index together.

## Response shape

This illustrative excerpt uses an [episode and transcript visible on Exa's public site](https://exa.ai/library/podcast/tpr3b0f359y/episode/s20kmckm3v9).
It shows the episode URL and timestamped text an agent can use; it is not a saved response from the
live Treg smoke test.

```json
{
  "results": [
    {
      "url": "https://exa.ai/library/podcast/tpr3b0f359y/episode/s20kmckm3v9",
      "title": "Carla Lalli Says Food Is A Feeling, from Expense Account",
      "text": "Yuri Lisordo 0:00\nHi, this is Yuri Lisordo, the managing producer..."
    }
  ]
}
```

Podcast searches use ordinary Exa search and content pricing. The amount can vary with the
request; use the actual charge reported by Treg for the call.
