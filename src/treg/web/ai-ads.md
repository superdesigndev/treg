# The AI ads playbook (2026), with Claude Code

Eleven steps for a marketer who does not know where to start. Each step is a prompt to type, what comes back, what to check and what to do next. Steps 1 to 8 were typed as written into Claude Code on 27 Nov 2026; steps 9 to 11 are method. The page: {BASE}/ai-ads

## How do you make ads with Claude Code?

To make ads with Claude Code, you describe each job in a prompt and the agent does the work in a folder on your computer: it pulls your competitors' ads from the public ad libraries, finds the videos that spread in your category, reads the pages those ads sell from, writes a one-page brief, and makes image ads and a short video from your product photo. You approve each step. The ads go into your ad account paused, through Meta's own connector, and you switch them on. This playbook is the eleven prompts, in order, with what came back when we ran them.

## Where to start (symptoms and steps)

- You do not know who you are up against. Go to step 1
- You check four ad libraries by hand, and nothing is saved. Go to step 2
- You cannot tell what works in your category. The method is a guess. Go to steps 3, 4
- You copy the ad and miss the offer behind it. Go to step 5
- You do not know what to make next. Go to step 6
- You need new ads this week and you are not a designer. Go to steps 7, 8
- You are afraid to let AI touch the ad account. Go to step 9
- Most of the budget goes to one ad, so you cannot read the rest. Go to step 10
- Your winners fade and nothing is ready to replace them. Go to steps 11, 3

## How do I start running ads with Claude Code?

1. Install Claude Code and open it in an empty folder.
2. Set up treg.to: paste `set up treg - https://treg.to/llms.txt` into Claude Code. A new verified account gets $1.00 of credit, once.
3. Add the skills: `npx skills add superdesigndev/treg`.
4. Save `product.md`:

```
What: [what you sell, in one line]
Who buys it: [who, and when they need it]
Price: [price and any offer]
Competitors: [three names and websites, or "I don't know"]
```

The example brand, Tidewell, is made up. The competitors in the runs are real and shown as Brand A, B and C.

## How do I find who advertises in my category?

Step 1 · Competitors · Run 27 Nov · $0.01

Symptom: You can name one or two competitors. You do not know who else is buying the same customers.

You have: product.md from the start page.

Type this:

```
Using treg, search the Meta ad library for active US ads matching "[your category word]".
Four pages. Group by advertiser: ads in the sample, days since the oldest started,
the page it links to. List the ones that do not sell what I sell separately.
Do not drop them. Save as advertisers.md.
```

You get: advertisers.md: a table of who is advertising. Ours had 59 ads from 29 advertisers, in two and a half minutes.

Check:

- Some rows are not competitors. One word matches many things; ours matched pharmacies, a food delivery app and bottled drinks.
- It is a sample. The advertiser with the most ads in four pages is not the biggest spender.

Next: Pick three names and write them in product.md.

treg does: Searches the Meta ad library by keyword and country, priced per page.

You decide: Which three are your real competitors.

## How do I get Meta, Google, TikTok and LinkedIn ad library data into Claude Code?

Step 2 · Ad libraries · Run 27 Nov · $0.06

Symptom: You open each ad library in a browser, scroll, screenshot, and lose it by next week.

You have: Three competitor names and their websites.

Type this:

```
Using treg, pull active US ads for [brand 1, site], [brand 2, site], [brand 3, site].
Meta ad library, Google Ads Transparency Center, TikTok ad library, LinkedIn ad library.
Stop at four pages each and $0.10. Show one table: returned, total, what is missing.
Then save every ad as swipe-[today].csv with library, ad id, brand, days since start,
format, headline, full copy, call to action, landing page, media link.
```

You get: swipe-[today].csv: one row per ad. Ours had 292 ads, in 4 minutes, for $0.06.

Check:

- Returned against total. Four pages is a sample; write both numbers down before you read an ad.
- The advertiser on every row. Searching by name returns other companies with similar names.
- TikTok. Its ad library does not cover US ads, so that column comes back empty. Step 3 covers TikTok another way.
- Blank copy. Google gives dates, format and an image, and no ad text. Some Meta catalog ads carry template text where the copy should be.

Free first. All four libraries are free in a browser, and that is enough for one brand, once. Meta’s own Ad Library API returns commercial ads only when they were shown in the EU. For US ads saved to a file every week, see the ways compared.

Next: Step 3 finds what spreads. Step 4 reads these ads closely.

treg does: Reads four ad libraries from one prompt, at about $0.002 a page.

You decide: How many pages. More pages cost more and take longer.

## How do I find the viral videos and hooks in my category?

Step 3 · Viral · Run 27 Nov · $0.11

Symptom: You scroll TikTok for ideas and call it research. Nothing is counted.

You have: Your category in two or three keywords.

Type this:

```
Use the make-ugc skill, research part only. Pull the trending TikTok and Instagram
videos for [your keywords]. US, English. For each top video: link, views, likes,
shares, creator, brand or creator account, marked as an ad or not. Write hooks.md:
the opening lines that repeat in the top videos and are missing from the weak ones.
Stop there. Make no image or video. Cap $0.50.
```

You get: hooks.md: the top videos with links, and five or six opening patterns with counts. Ours read 129 TikTok videos and 23 reels, in six and a half minutes, for $0.11.

Check:

- Big views with few likes is usually paid reach. Rank by shares and like rate, not views.
- It reads captions and transcripts. It does not watch the videos. Open the top five yourself.
- Organic is not paid. A hook that spreads for a creator is a guess for an ad until you test it.

One competitor’s own account. To rank a single brand’s posts, type: “Use the rea-growth skill in content mode. Target: [competitor site]. Channels: TikTok and Instagram. Cap $1.” Ours ranked 120 posts in 8 minutes for $0.01. For a large brand the top posts were mostly partnerships and giveaways: useful to know, hard to copy.

Next: Keep hooks.md. The brief in step 6 reads it.

treg does: Searches TikTok and Instagram by keyword and returns views, likes, shares and transcripts.

You decide: Which patterns fit your product. The count does not know your customer.

## How do I tell why a competitor’s ad works?

Step 4 · Teardown · Run 27 Nov

Symptom: You saved 200 ads and see no pattern.

You have: swipe-[today].csv from step 2.

Type this:

```
From the saved pull, take [brand]'s three oldest video ads on Meta. Remove duplicate
copy first. For each: take a frame every three seconds and describe the first frame,
the words on screen, the offer, who is on camera. Then list what repeats across all
three. Save as teardown.md.
```

You get: teardown.md: three ads taken apart frame by frame, and what they share. Ours took 5 minutes and no treg charge. In all three ads the product was in the first frame, the brand was readable on the pack, and no price was shown.

Check:

- Say long-running, not winning. A start date does not show profit, and an old ad can be neglect.
- One long run means little. The same idea in several live versions, with the same offer, means more.
- It reads frames, not sound. Listen to the opening yourself.

Next: Note the format and the offer. Step 5 checks the offer.

treg does: Supplies the media link for each ad. Claude Code reads the frames on your machine.

You decide: What to borrow. Take the structure. Never rebuild their ad with your logo: the library lets you look, and gives you no right to the creative, the actor or the music.

## What is the offer behind the ad?

Step 5 · Offer · Run 27 Nov · under $0.01

Symptom: You copy the ad and miss the price, the bundle and the shipping line that make it convert.

You have: swipe-[today].csv from step 2.

Type this:

```
For each brand in the saved pull, find the page most of its ads link to. Read it.
Give price, pack size, subscription offer, shipping line. If a link is dead, say
which page you read instead. Then list three gaps [my brand] could use.
Save as offers.md.
```

You get: offers.md: a price and offer table for each competitor, and three gaps. Ours read three pages in 5 minutes, for under a cent. Per stick, the three brands charged $1.20 to $1.56 for a single order and $0.99 to $1.30 on subscription.

Check:

- Sale weeks. Ours ran in Black Friday week, with a 30% code live on one site.
- Blocked pages. One site refused to load; Claude said so and named the page it read instead.
- Compare like with like: price per unit, on the pack size most ads link to.

Next: You now have hooks.md, teardown.md and offers.md. That is the research.

treg does: Reads a web page for a fraction of a cent. A dead link is free.

You decide: Which gap is true for your product.

## How do I write a creative brief with Claude Code?

Step 6 · Brief · Run 27 Nov · $0

Symptom: You go from a folder of competitor ads straight to making things, and every ad says three things at once.

You have: product.md, offers.md, hooks.md.

Type this:

```
Read product.md, offers.md and hooks.md. Write brief.md, one page: who the ad is for
and the problem it solves, then three angles. For each angle: the hook pattern it
borrows from hooks.md, the format (static or video), the opening line, and the one
claim it makes. Use only facts in product.md. No health claims, no invented reviews
or customers. Mark the angle to test first and say why. Then stop.
```

You get: brief.md: three angles, one marked first. Ours took under a minute.

Check:

- Every claim is in product.md. If the brief says something you did not write, delete it.
- One angle is one person with one problem. Not three benefits in one ad.
- You pick. Claude ranked the angles; you know the customer.

Next: The static angle goes to step 7. The video angle goes to step 8.

treg does: Nothing here. This step reads your own files.

You decide: The claims. Paste 20 of your own reviews into product.md first: angles from customers’ words beat angles from competitors’ ads.

## Can Claude Code make static image ads from a product photo?

Step 7 · Image ads · Run 27 Nov · $0

Symptom: You need image ads this week. A freelancer takes days, and AI tools redraw your pack wrong.

You have: brief.md and a product photo saved as product.png.

Type this:

```
Here is my product photo (product.png). From brief.md, take the static angle and
write five headlines, using only my facts. Show me the five lines. When I approve,
make one square ad. Keep the pack as it is. No reviews, no ratings. Show me, then wait.
```

Then: "Approved. Make the first ad."

Then: "Good. Make the other four."

You get: Five square image ads in a folder. Ours took 2 minutes and cost nothing: Claude Code set each headline over our own photo, so the pack is the real one.

Check:

- Every word on the ad, and the small print on your pack.
- One first, then the batch. Fix the layout once, not five times.

Next: Go to step 9.

treg does: Image models at about $0.03 an image, when you ask for a scene: “use an image model, on a kitchen counter”.

You decide: The words. You write the claim; the model draws the layout.

## Can Claude Code make a UGC-style video ad?

Step 8 · Video · Run 27 Nov · $2.19

Symptom: A creator video costs hundreds of dollars and a week, and you do not know yet if the angle works.

You have: brief.md, hooks.md, product.png.

Type this:

```
Use the make-ugc skill. From brief.md, take the video angle marked first. Make a still
of the opening frame with my product (product.png) and show me. Write the script:
8 seconds, the opening line from the brief, facts from product.md only, no personal
experience, no health result. Tell me the price. Wait for my yes before any video.
Cap $3.
```

Then: "Approved: the still, the script and the price. Make the clip."

You get: One 8-second vertical clip with sound. Ours took 12 minutes and $2.19: two stills at $0.025 each and one take at $2.14. Claude redid the still once on its own, because the first had a tripod in shot.

Check:

- The still, before you pay for video. Our take cost about 85 times a still.
- Play the clip with sound. Listen for your brand name.
- Count the hands and the products in every second.

What it is. A cheap test of an angle, not a finished ad. Advertisers who tested both say the AI version finds the angle and a filmed remake lasts longer.

Next: Go to step 9.

treg does: Video models priced per clip, quoted before the call.

You decide: The script. No invented testimonial, no health result. Label an AI-made person as AI. TikTok requires it for realistic AI content. Meta requires it only for political ads.

## How do I connect Claude Code to my Meta ad account safely?

Step 9 · Launch · Method

Symptom: You are afraid an agent will spend money, change a budget or get the account restricted.

You have: Your ads, and a Meta ad account you can log in to.

Set up once. Connect Meta’s own ads connector. It is made by Meta and free. Search “Meta ads AI connectors” in Meta’s Business Help Centre and follow it. It is an open beta, and not every account has every tool yet.

Type this:

```
Using the Meta ads connector, read my account first and change nothing. Show me my
campaigns and what each spent in the last 7 days. Then write a plan: one new campaign
for testing, one ad set, my five image ads and one video, [$X] a day. Show me the
plan. When I approve, create everything paused. Turn nothing on.
```

You get: A plan, then a paused campaign you can open in Ads Manager. We have not run this step ourselves; it follows Meta’s documentation and the open-source ads skills.

Check:

- In Ads Manager, before you switch it on: the Page, the link, the budget, the country, each ad’s text.
- New ads sit in their own ad set. Next to an old winner they get no spend.
- In Meta Business settings, block the actions you do not want and cap the budget the connector can set.

Google. Google’s own connector reads your account. It cannot create or change campaigns, ads, budgets or bids, so upload there by hand.

Is it safe? Meta’s help page says using its connector alone does not put an account at risk. A few advertisers report restrictions after connecting an agent, and none has shown the cause. Start read-only for a week.

Next: Read first. Then a plan. Then paused. You switch it on.

treg does: Not the best route here. Meta’s connector is free and official, so use it.

You decide: When the ads go live, and every budget. The agent never changes a budget or a bid without your yes.

## Which ads do I keep and which do I kill?

Step 10 · Keep or kill · Method

Symptom: Meta puts most of the budget on one ad, results swing day to day, and you kill ads on a feeling.

You have: Ads that have run for at least four days.

Type this:

```
Using the Meta ads connector, read only. For each ad in the test campaign over the
last 7 days: spend, purchases, cost per purchase, click rate. My target cost per
purchase is [$X]. Sort into: keep, kill, too early. "Too early" means it has spent
less than twice my target. If tracking looks broken, stop and tell me. Change nothing.
```

You get: Three lists: keep, kill, too early. We have not run this step ourselves.

Check:

- Tracking first. If purchases are zero everywhere, the ads are not the problem.
- Most of the budget goes to one ad. The rest are too early, not losers.
- “Twice your target, and not before four days” is what media buyers do. It is not a Meta rule, and they disagree on the number.

Next: Pause the kills yourself. Move a keeper to its own ad set. Write one line in results.md: which angle won.

treg does: Nothing here. Your results come from your own account.

You decide: The target cost, and every pause.

## What should I do every week?

Step 11 · Weekly · Method

Symptom: A winner fades and nothing is ready to replace it.

You have: Last week’s swipe file, brief.md, results.md.

Type this:

```
Pull the same three brands again and save as swipe-[today].csv. Compare with last
week's file: which ads are new, which are not seen any more. Then read results.md and
write next week's brief: keep the angle that won, replace the one that lost with a
new one from hooks.md.
```

You get: What changed at your competitors, and a new brief. We have not run this step ourselves.

Check:

- “Not seen” is not “stopped”, unless both pulls were complete.
- Do not make new ads because a week went by. If an ad still hits your target, leave it.

Next: Back to step 7 with the new brief.

treg does: The same pull as step 2, for a few cents. treg.to does not schedule; your agent or you start it.

You decide: Whether anything needs replacing. Make new ads when cost rises while the same people keep seeing the old one.

## Ways to get ad library data into Claude

Read on 26 Nov 2026 from each provider's own pages. We ran only the last row.

| Way | What you get | Cost | The catch |
|---|---|---|---|
| The ad libraries in a browser | Every ad, with its creative | Free | By hand. Nothing saved |
| Meta’s ads connector | A search of the Meta ad library from Claude | Free | Meta only. Not every account has it yet |
| Meta’s Ad Library API | Political ads worldwide; any ad shown in the EU | Free | No US commercial ads. Needs identity confirmation and code |
| A scraper marketplace such as Apify | Meta ads by Page, keyword or country | About $5 per 1,000 ads | One scraper per platform |
| A swipe-file product such as Foreplay | Saved ads on shared boards | Monthly plans | A seat product for teams |
| treg.to | Meta, Google, TikTok and LinkedIn libraries and trending videos, from one prompt | About $0.002 a page; our 132 Meta ads cost $0.017 | Pay per call. No boards |

Rule: start free. Pay when you repeat it.

## Claude Code skills and connectors for ads, and what each needs from you

| Repository | Stars | What it does | What you bring |
|---|---|---|---|
| coreyhaines31/marketingskills | 62.9k | Ads and ad-creative skills, audits, decision rules | Nothing for the skills; Meta’s connector or your own tokens for live data |
| AgriciDaniel/claude-ads | 11.4k | Account audit and planning | Exports or screenshots you paste in. It does not write to accounts |
| googleads/google-ads-mcp | 1.2k | Google’s own connector: reads, and writes assets and labels only | A Google Cloud project with the Google Ads API enabled |
| pipeboard-co/meta-ads-mcp | 1.4k | Meta read and write tools, self-hosted or hosted | Your own Meta developer app and token, or a paid account |
| kLOsk/adloop | 304 | Google, Reddit and Microsoft Ads with a preview before every write | Your own Google Cloud project |
| sepivip/meta-ads-skill | 15 | A launch playbook for Meta’s own connector | Meta’s connector and an ad account with a payment method |

## Every run behind this playbook

| Run | Date | What came back | Metered on treg.to |
|---|---|---|---|
| Steps 1 to 8, each printed prompt typed as written, in order, in one empty folder (11 prompts, 38 minutes) | 27 Nov | advertisers.md, a 292-ad swipe file, hooks.md, teardown.md, offers.md, brief.md, five image ads, one clip | $2.37 |
| Step 3 on one competitor’s own account (rea-growth) | 26 Nov | 120 posts ranked | $0.01 |
| First pass, scripted, all research and creative steps | 26 Nov | 251 ads, three landing pages, five ads, three clips (one refused, one rejected) | $1.99 |

## Glossary and questions

- **Ad library**: The public page where a platform shows the ads running on it. Meta, Google, TikTok and LinkedIn each have one.
- **Swipe file**: Your saved examples of other people’s ads (step 2).
- **Hook**: The opening line or first second of an ad (step 3).
- **Angle**: One person, one problem, one claim (step 6).
- **Brief**: One page that says who the ad is for, the angle, the opening line and the format (step 6).
- **UGC**: Creator-style video. In this playbook it is made by AI and labelled as such (step 8).
- **Connector**: A link between Claude and another service. Also called an MCP server (step 9).
- **Ad set**: The group inside a campaign that holds ads and a budget (step 9).
- **Cost per purchase**: Spend divided by purchases. The number step 10 sorts on.

### Can Claude Code see my competitors' ads?

Yes, the public ones. Meta, Google, TikTok and LinkedIn each publish an ad library. Claude Code can read them through a data tool such as treg.to and save the ads to a file. It sees the creative, the copy, the start date and the landing page. It does not see spend or results: no library publishes those for commercial ads.

### Does a long-running ad mean it is winning?

Not on its own. A start date shows the ad was not turned off. It does not show profit, and an old ad can be neglect. The stronger sign is the same idea running in several live versions, with the same offer and landing page, still there when you check again a week later.

### Is it safe to connect Claude to my Meta ad account?

Meta's help page says using its own ads connector does not by itself put an account at risk; restrictions follow policy violations, payment problems and compromised accounts. A few advertisers report restrictions after connecting an agent, and none has shown the cause. Start read-only, create everything paused, and set the connector's action and budget limits in Meta Business settings.

### How much does it cost to run this playbook?

In our 27 Nov 2026 run the research steps (1 to 6) cost $0.18 on treg.to, the image ads were free because Claude Code laid out our own product photo, and one 8-second video cost $2.19. Claude Code's own usage and your ad spend are separate.

### Do AI-made ads perform as well as real ones?

We have not measured it, and no source we read gives a reliable answer. Advertisers who posted their own tests describe AI ads as a cheap way to find an angle, with a filmed remake of the winner lasting longer. Treat the ads from steps 7 and 8 as tests.
