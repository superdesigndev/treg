# video-shot-breakdown

A shot-by-shot breakdown of an X video post, the way an editor studies one: where every cut is,
what each shot shows and says, a keyframe image for each, and a read on the hook, structure and
pacing.

1. `tikhub.x.twitter-web-fetch-tweet-detail` resolves the post to its mp4 files, its length and
   its text and counts. A video over 120 s is refused before anything else is spent.
2. `google-ai.video-understand.gemini-3-8-flash` watches the 720p file at 5 frames a second and
   high resolution, with low thinking, and answers one JSON document: the shots and the analysis.
   At these settings cuts land within about 0.1 s and animated numbers are read once they settle;
   at the model's default of 1 frame a second they land within about 0.3 s and a counter can be
   read mid-count. If Google refuses the 720p file (its fetch limit is 15 MB), the next size down
   is tried once.
3. Each shot's `keyframe_url` is a Cloudflare frame of the same mp4 at the moment the model picked
   inside the shot, after its animations settle. Nothing is downloaded or stored: the image is
   made when it is first opened, and it stops working if the post is deleted.

`language` sets the language of the descriptions and the analysis (`en`, `zh`, ...); on-screen
text is always copied verbatim.

Measured on two posts: a 40 s product video (13 shots) took about 40 s and $0.06; an 82 s video
with 45 hard cuts (52 shots) took about 65 s and $0.11, with 44 of its 45 cuts within 0.5 s of
ffmpeg's. The cost is the two calls' own price; the tool adds no fee.
