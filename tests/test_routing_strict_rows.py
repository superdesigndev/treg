"""`X-Treg-Route-Strict-Filters` judges rows: a row whose own field breaks a sent filter leaves
`output`, `raw` keeps the provider's body whole, and every answered child is billed as usual."""

from __future__ import annotations

import json
from datetime import date

import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import row_filters as R
from treg.domain.catalog.routing.contracts import parse_contracts

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider

TODAY = date(2026, 11, 28)


def _apply(key, test, fields, sent, rows):
    return R.apply((R.RowFilter(key, test, tuple(fields)),), {key: sent}, {key}, rows, TODAY)


# (asked, row, verdict): True keeps it as a match, None keeps it unchecked, False removes it.
PLACES = [
    # a country, a state or a metro asked: a row inside it is never removed
    ("United States", "Austin, TX", True),
    ("United States", "Austin, Texas, United States", True),
    ("Germany", "Berlin", None),
    ("Germany", "Berlin, Germany", True),
    ("California", "Greater Los Angeles Area", None),
    ("California", "San Francisco, CA", True),          # a US ask: the code is the state
    ("California", "San Francisco, CA, US", True),
    ("California", "San Francisco, California, United States", True),
    ("New York City", "New York, New York, United States", True),
    ("New York City", "New York City Metropolitan Area", True),
    ("New York", "Brooklyn, NY", True),
    ("Greater Phoenix", "Scottsdale, Arizona", None),
    ("Greater Phoenix", "Phoenix, Arizona, United States", True),
    ("Texas", "Texas", True),
    ("Austin, Texas", "Texas, United States", None),        # only a larger place: not judged
    ("Austin, Texas", "United States", None),
    ("Indiana", "Columbus, IN", True),                       # a US ask: the code is the state
    ("Indiana", "Columbus, IN, US", True),
    ("Germany", "Berlin, DE", None),                         # DE is also Delaware
    ("United States", "Columbus, IN", True),
    ("United Kingdom", "Austin, MN", None),                  # abroad a code never removes
    # the city cases
    ("Paris, France", "Paris, FR", True),
    ("France", "Paris, FR", True),
    ("Oregon, United States", "Portland, OR", True),
    ("Austin, Texas", "Austin, TX, US", True),
    ("Austin, Texas", "Greater Austin Area", True),
    ("Zürich, Switzerland", "Zurich, Switzerland", True),
    ("São Paulo", "Sao Paulo, SP, Brazil", True),
    ("London, United Kingdom", "Greater London, England, United Kingdom", True),
    # a different place at the same level: removed
    ("United States", "Berlin, Germany", False),
    ("Germany", "Austin, TX, US", False),
    ("California", "Austin, Texas, United States", False),
    ("California", "Austin, TX", False),
    ("Austin, Texas", "Dallas, Texas, United States", None),   # another city part: never judged
    ("Austin, Texas", "Austin, MN", False),
    ("London, United Kingdom", "Manchester, England", None),  # outside the US only the country is judged
    ("Paris, France", "Lyon, France", None),
    # a city with nothing around it says nothing about a row elsewhere
    ("London", "Berlin, Germany", None),
    ("Greater Phoenix", "Berlin, Germany", None),
]


@pytest.mark.parametrize("asked,row,verdict", PLACES)
def test_place_removes_only_a_row_that_names_another_place_at_the_level_asked(asked, row, verdict):
    assert R.place(asked, row) is verdict


# A second table: "keep" may be a match or unchecked, "remove" must be removed.
KEEP_OR_REMOVE = [
    ("Austin, TX", "Paris, France", "remove"),
    ("Berlin", "Toronto, Canada", "remove"),
    ("Berlin, Germany", "Toronto, Ontario, Canada", "remove"),
    ("Austin, Texas", "Austin, TX, United States", "keep"),
    ("Austin, TX", "Dallas, TX", "remove"),
    ("Austin, TX", "Texas, United States", "keep"),
    ("Austin, TX", "Greater Austin Area", "keep"),
    ("Austin, TX", "Austin, Texas Metropolitan Area", "keep"),
    ("Texas", "Houston, TX", "keep"),
    ("Texas", "Sacramento, CA", "remove"),
    ("California", "San Francisco Bay Area", "keep"),
    ("California", "Los Angeles, California, United States", "keep"),
    ("CA", "San Jose, CA", "keep"),
    ("California", "Toronto, ON, Canada", "remove"),
    ("Canada", "Toronto, Ontario, Canada", "keep"),
    ("Canada", "Vancouver, BC", "keep"),
    ("Canada", "Seattle, WA", "remove"),
    ("Indiana", "Indianapolis, IN", "keep"),
    ("India", "Bengaluru, Karnataka, India", "keep"),
    ("India", "Indianapolis, IN", "remove"),
    ("IN", "Mumbai, India", "keep"),
    ("Georgia", "Atlanta, GA", "keep"),
    ("Georgia", "Tbilisi, Georgia", "keep"),
    ("Atlanta, GA", "Tbilisi, Georgia", "remove"),
    ("Atlanta, Georgia", "Atlanta, GA, United States", "keep"),
    ("San Francisco, CA", "San Francisco Bay Area", "keep"),
    ("San Francisco", "Bay Area", "keep"),
    ("London", "Greater London", "keep"),
    ("London, UK", "London, England, United Kingdom", "keep"),
    ("London, United Kingdom", "London, Ontario, Canada", "remove"),
    ("London", "Manchester, England", "keep"),
    ("United Kingdom", "Greater London", "keep"),
    ("United Kingdom", "Paris, Île-de-France, France", "remove"),
    ("Dallas, TX", "DFW", "keep"),
    ("Dallas, TX", "Dallas-Fort Worth Metroplex", "keep"),
    ("Texas", "DFW", "keep"),
    ("Munich", "München, Bayern, Deutschland", "keep"),
    ("München", "Munich, Bavaria, Germany", "keep"),
    ("Munich, Germany", "München, Germany", "keep"),
    ("Cologne", "Köln, Germany", "keep"),
    ("Köln, Germany", "Cologne, North Rhine-Westphalia, Germany", "keep"),
    ("Germany", "Köln, Nordrhein-Westfalen, Deutschland", "keep"),
    ("Germany", "Berlin, Germany", "keep"),
    ("Germany", "Vienna, Austria", "remove"),
    ("New York", "Brooklyn, NY", "keep"),
    ("New York, NY", "New York City Metropolitan Area", "keep"),
    ("New York, NY", "Jersey City, NJ", "remove"),
    ("New York", "Remote", "keep"),
    ("Austin, TX", "Remote - US", "keep"),
    ("United States", "Remote", "keep"),
    ("United States", "Toronto, Canada", "remove"),
    ("US", "Austin, TX", "keep"),
    ("USA", "London, UK", "remove"),
    ("Paris", "Paris, TX", "keep"),
    ("Paris, France", "Paris, TX", "remove"),
    ("Portland, OR", "Portland, ME", "remove"),
    ("Washington", "Seattle, WA", "keep"),
    ("Washington, DC", "Washington, District of Columbia", "keep"),
    ("Ontario, Canada", "Toronto, ON", "keep"),
    ("São Paulo, Brazil", "Sao Paulo, SP, Brasil", "keep"),
    ("Zurich, Switzerland", "Zürich, Schweiz", "keep"),
    ("Remote", "Austin, TX", "keep"),
    ("Minnesota", "Minneapolis, MN", "keep"),
    ("Mongolia", "Minneapolis, MN", "remove"),
    ("California", "Hamburg, Germany", "remove"),
]
KEEP_OR_REMOVE += [
    # state-named cities, other forms
    ("Washington DC", "Washington, DC, USA", "keep"),
    ("Washington DC", "Washington, District of Columbia, United States", "keep"),
    ("Washington, D.C.", "Washington, DC", "keep"),
    ("Washington, D.C.", "Washington, D.C., United States", "keep"),
    ("Washington, D.C.", "Seattle, WA", "remove"),
    ("Washington, DC", "Seattle, Washington, United States", "remove"),
    ("Washington, DC", "Arlington, VA", "remove"),
    ("Kansas City, Kansas", "Kansas City, KS", "keep"),
    ("Kansas City, Kansas", "Kansas City, Kansas, United States", "keep"),
    ("Kansas City, Kansas", "Kansas City, MO", "remove"),
    ("Kansas City, MO", "Kansas City Metropolitan Area", "keep"),
    ("Kansas City, MO", "Wichita, Kansas", "remove"),
    ("Oklahoma City", "Oklahoma City, OK", "keep"),
    ("Oklahoma City", "Oklahoma City, Oklahoma, United States", "keep"),
    ("Oklahoma City, OK", "Tulsa, Oklahoma", "remove"),
    ("Oklahoma City, OK", "Oklahoma", "keep"),
    ("Indianapolis, Indiana", "Indianapolis, IN", "keep"),
    # New York city vs state
    ("New York", "New York, NY", "keep"),
    ("New York", "New York City Metropolitan Area", "keep"),
    ("New York", "Brooklyn, New York, United States", "keep"),
    ("New York, NY", "New York, New York, United States", "keep"),
    ("New York, NY", "Buffalo, NY", "remove"),
    ("New York, NY", "Jersey City, NJ", "remove"),
    ("New York City", "New York, NY 10001", "keep"),
    ("New York", "London, United Kingdom", "remove"),
    ("New York", "Los Angeles, California", "remove"),
    # Georgia country vs state
    ("Georgia", "Atlanta, GA", "keep"),
    ("Georgia", "Tbilisi, Georgia", "keep"),
    ("Atlanta, Georgia", "Atlanta, GA, United States", "keep"),
    ("Atlanta, Georgia", "Savannah, Georgia", "remove"),
    ("Tbilisi, Georgia", "Tbilisi, Georgia", "keep"),
    ("Tbilisi, Georgia", "Tbilisi", "keep"),
    ("Atlanta, GA", "Tbilisi, Georgia", "remove"),
    # three-part asks
    ("Austin, Texas, United States", "Austin, TX", "keep"),
    ("Austin, Texas, United States", "Austin, Texas Metropolitan Area", "keep"),
    ("Austin, Texas, United States", "Dallas, TX", "remove"),
    ("Austin, Texas, United States", "Austin, Minnesota, United States", "remove"),
    ("Austin, Texas, United States", "Berlin, Germany", "remove"),
    ("Portland, Oregon, USA", "Portland, ME", "remove"),
    ("Portland, Oregon, USA", "Portland, OR 97201", "keep"),
    # US cities without a state
    ("Chicago", "Chicago, IL", "keep"),
    ("Chicago", "Chicago, Illinois, United States", "keep"),
    ("Chicago", "Greater Chicago Area", "keep"),
    ("Seattle", "Seattle, WA 98101", "keep"),
    ("Boston", "Boston, MA, US", "keep"),
    ("Denver", "Denver Metropolitan Area", "keep"),
    # non-US with regions
    ("Toronto, Ontario", "Toronto, ON, Canada", "keep"),
    ("Toronto, Ontario", "Toronto, Ontario, Canada", "keep"),
    ("Toronto, Ontario", "Vancouver, British Columbia, Canada", "keep"),  # other city, judged only by country: unchecked ok
    ("Toronto, Ontario, Canada", "Austin, Texas", "remove"),
    ("Toronto, Ontario, Canada", "London, United Kingdom", "remove"),
    ("Manchester, UK", "Manchester, England, United Kingdom", "keep"),
    ("Manchester, UK", "Manchester, Greater Manchester, UK", "keep"),
    ("Manchester, UK", "Manchester, NH", "remove"),
    ("Manchester, UK", "Paris, France", "remove"),
    ("Sydney, NSW", "Sydney, New South Wales, Australia", "keep"),
    ("Sydney, NSW", "Sydney NSW 2000", "keep"),
    ("Sydney, NSW, Australia", "Sydney, Nova Scotia, Canada", "remove"),
    ("Sydney, NSW, Australia", "Melbourne, Victoria, Australia", "keep"),
    ("Munich, Germany", "München, Bayern, Deutschland", "keep"),
    # clear mismatches
    ("Germany", "Paris, France", "remove"),
    ("France", "Lyon, France", "keep"),
    ("United States", "Toronto, Canada", "remove"),
    ("California", "Austin, Texas", "remove"),
    ("California", "Los Angeles, CA", "keep"),
    ("Texas", "Houston, Texas, United States", "keep"),
    ("Illinois", "Chicago, IL", "keep"),
    ("Illinois", "Milwaukee, WI", "remove"),
    ("San Diego, CA", "San Diego, California, United States", "keep"),
    ("San Diego, CA", "London, England", "remove"),
    ("Lima, Ohio", "Lima, Peru", "remove"),
    ("Lima, Peru", "Lima, OH", "remove"),
    ("Paris, Texas", "Paris, France", "remove"),
    ("Paris, France", "Paris, TX", "remove"),
    ("Paris", "Paris, Île-de-France, France", "keep"),
    ("Remote", "Austin, TX", "keep"),
]
KEEP_OR_REMOVE += [
    # city-part rule: regions, counties, neighbourhoods, spellings
    ("Santa Monica, California", "Los Angeles Metropolitan Area, California", "keep"),
    ("San Jose, California", "San Francisco Bay Area, CA", "keep"),
    ("San Francisco, California", "Bay Area, CA", "keep"),
    ("Reston, VA", "Fairfax County, Virginia", "keep"),
    ("Austin, TX", "Travis County, TX", "keep"),
    ("Austin, TX", "Austin, Travis County, TX", "keep"),
    ("New York, NY", "Brooklyn, NY", "keep"),
    ("New York, NY", "Manhattan, New York, United States", "keep"),
    ("Manhattan, New York", "New York, NY", "keep"),
    ("Los Angeles, CA", "Hollywood, California", "keep"),
    ("St. Louis, MO", "Saint Louis, Missouri", "keep"),
    ("Saint Paul, MN", "St Paul, Minnesota, United States", "keep"),
    ("Fort Worth, TX", "Ft. Worth, TX", "keep"),
    ("Mount Pleasant, SC", "Mt Pleasant, South Carolina", "keep"),
    ("Salt Lake City, UT", "SLC, Utah", "keep"),
    ("Winston-Salem, NC", "Winston Salem, North Carolina", "keep"),
    ("Durham, NC", "Raleigh-Durham-Chapel Hill Area", "keep"),
    ("Wilkes-Barre, PA", "Wilkes Barre, Pennsylvania, US", "keep"),
    ("Dallas, TX", "Fort Worth, Texas", "remove"),
    ("Cambridge, MA", "Somerville, Massachusetts", "remove"),
    ("San Diego, California", "Sacramento, CA", "remove"),
    ("Phoenix, AZ", "Tucson, Arizona, United States", "remove"),
    # extra parts, postal codes, reversed order
    ("Austin, TX", "Austin, Texas Metropolitan Area, US", "keep"),
    ("Austin, TX", "US, Texas, Austin", "keep"),
    ("San Francisco, CA", "United States, California, San Francisco", "keep"),
    ("Austin, TX", "78701", "keep"),
    ("Austin, TX", "78701, TX", "keep"),
    ("Austin, TX", "1100 Congress Ave, Austin, TX 78701", "keep"),
    ("Austin, TX", "Downtown, Austin, Texas", "keep"),
    ("Austin, TX", "Houston, TX 77002", "remove"),
    ("Columbus, Ohio", ["Ohio - Columbus, Three Nationwide Plaza"], "keep"),
    # several places in one value (job postings)
    ("San Francisco, California", "New York, NY (HQ); San Francisco, CA", "keep"),
    ("New York, NY", "New York, NY (HQ); San Francisco, CA", "keep"),
    ("Dallas, TX", ["Austin, TX", "Dallas, TX"], "keep"),
    ("New York, NY", ["San Francisco, CA", "New York, NY"], "keep"),
    ("Seattle, WA", ["Portland, OR", "Denver, CO"], "remove"),
    # province / region codes that are ISO country codes
    ("Saskatoon, Canada", "Saskatoon, SK, Canada", "keep"),
    ("Canada", "Regina, SK, Canada", "keep"),
    ("St. John's, Newfoundland, Canada", "St. John's, NL, Canada", "keep"),
    ("Monterrey, Mexico", "Monterrey, NL, Mexico", "keep"),
    ("Pune, India", "Pune, MH, India", "keep"),
    ("India", "Gurugram, HR, India", "keep"),
    ("Brazil", "Curitiba, PR, Brazil", "keep"),
    ("Belo Horizonte, Brazil", "Belo Horizonte, MG, Brazil", "keep"),
    ("Adelaide, Australia", "Adelaide, SA, Australia", "keep"),
    ("Italy", "Torino, TO, Italy", "keep"),
    ("Chennai, India", "Chennai, TN, India", "keep"),
    ("Toronto, Canada", "Toronto, ON, Canada", "keep"),
    # non-Latin and mixed case
    ("Moscow, Russia", "Москва, Россия", "keep"),
    ("Moscow, Russia", "Москва, Russia", "keep"),
    ("Tokyo, Japan", "東京都, Japan", "keep"),
    ("Beijing, China", "北京市 100000, China", "keep"),
    ("Tokyo, Japan", "Seoul, South Korea", "remove"),
    ("AUSTIN, TEXAS", "austin, tx, united states", "keep"),
    ("austin, tx", "DALLAS, TEXAS", "remove"),
    ("Zürich, Switzerland", "ZURICH, CH", "keep"),
    ("Łódź, Poland", "Lodz, Poland", "keep"),
    # codes shared by a state and a country
    ("Bogota, CO", "Bogotá, Colombia", "keep"),
    ("Bogota, CO", "Denver, CO", "keep"),
    ("Denver, CO", "Medellín, Colombia", "keep"),
    ("Denver, Colorado", "Boulder, CO", "remove"),
    ("Buenos Aires, AR", "Buenos Aires, Argentina", "keep"),
    ("Little Rock, AR", "Little Rock, Arkansas", "keep"),
    ("Berlin, DE", "Berlin, Germany", "keep"),
    ("Berlin, Germany", "Berlin, DE", "keep"),
    ("Dover, DE", "Wilmington, Delaware", "remove"),
    ("Savannah, GA", "Savannah, Georgia, US", "keep"),
    ("Libreville, GA", "Libreville, Gabon", "keep"),
    ("Atlanta, GA", "Kutaisi, Georgia", "remove"),
    ("Rabat, MA", "Rabat, Morocco", "keep"),
    ("Boston, MA", "Boston, Massachusetts", "keep"),
    ("Boston, Massachusetts", "Worcester, MA", "remove"),
    ("Chisinau, MD", "Chișinău, Moldova", "keep"),
    ("Baltimore, MD", "Baltimore, Maryland, USA", "keep"),
    ("Podgorica, ME", "Portland, Maine", "remove"),
    ("Portland, ME", "Portland, Maine, United States", "keep"),
    ("Ulaanbaatar, MN", "Minneapolis, Minnesota", "remove"),
    ("Minneapolis, MN", "Minneapolis, Minnesota", "keep"),
    ("Panama City, PA", "Panama City, Panama", "keep"),
    ("Pittsburgh, PA", "Pittsburgh, Pennsylvania", "keep"),
    ("Panama City, Florida", "Panama City, Panama", "remove"),
    ("Victoria, SC", "Victoria, Seychelles", "keep"),
    ("Columbia, SC", "Columbia, Missouri", "remove"),
    ("Montgomery, AL", "Tirana, Albania", "keep"),
    ("Tirana, Albania", "Montgomery, AL", "keep"),
    ("Baku, AZ", "Baku, Azerbaijan", "keep"),
    ("Phoenix, AZ", "Phoenix, Arizona", "keep"),
    ("Omaha, NE", "Omaha, Nebraska", "keep"),
    ("Niamey, NE", "Niamey, Niger", "keep"),
    ("Richmond, VA", "Richmond, Virginia, US", "keep"),
    ("Richmond, VA", "Richmond, British Columbia, Canada", "remove"),
]
KEEP_OR_REMOVE += [
    # a last-part two-letter (or three-letter) region code abroad
    ("Toronto, Canada", "Toronto, ON", "keep"), ("Vancouver, Canada", "Vancouver, BC", "keep"),
    ("Melbourne, Australia", "Melbourne, VIC", "keep"), ("Edinburgh, United Kingdom", "Edinburgh, SC", "keep"),
    ("Edinburgh, Scotland", "Glasgow, SC", "keep"), ("Guadalajara, Mexico", "Guadalajara, JA", "keep"),
    ("Perth, Australia", "Perth, WA", "keep"), ("Adelaide, Australia", "Adelaide, SA", "keep"),
    ("Darwin, Australia", "Darwin, NT", "keep"), ("Regina, Canada", "Regina, SK", "keep"),
    ("St. John's, Canada", "St. John's, NL", "keep"), ("Charlottetown, Canada", "Charlottetown, PE", "keep"),
    ("Monterrey, Mexico", "Monterrey, NL", "keep"), ("Chennai, India", "Chennai, TN", "keep"),
    ("Mumbai, India", "Mumbai, MH", "keep"), ("Belo Horizonte, Brazil", "Belo Horizonte, MG", "keep"),
    ("Curitiba, Brazil", "Curitiba, PR", "keep"), ("Porto Alegre, Brazil", "Porto Alegre, RS", "keep"),
    ("Munich, Germany", "Munich, BY", "keep"), ("Palermo, Italy", "Palermo, PA", "keep"),
    ("Genova, Italy", "Genova, GE", "keep"), ("Ottawa, ON", "Gatineau, Quebec, Canada", "keep"),
    ("London, United Kingdom", "Leeds, UK", "keep"), ("Austin, TX", "London, UK", "remove"),
    # three parts ending in a region code
    ("Toronto, Canada", "Toronto, Ontario, ON", "keep"), ("Perth, Australia", "Perth, Western Australia, WA", "keep"),
    ("Hyderabad, India", "Hyderabad, Telangana, TG", "keep"), ("São Paulo, Brazil", "São Paulo, São Paulo, SP", "keep"),
    ("Austin, TX", "Round Rock, Williamson, TX", "keep"), ("Austin, TX", "Portland, Multnomah, OR", "remove"),
    ("Perth, Australia", "Fremantle, Perth, WA", "keep"), ("Perth, WA, Australia", "Fremantle, WA", "keep"),
    # states and countries in other languages
    ("Austin, Texas", "Austin, Texas, Estados Unidos", "keep"), ("Los Angeles, CA", "Los Ángeles, California, EE. UU.", "keep"),
    ("San Francisco, CA", "San Francisco, Kalifornien, Vereinigte Staaten", "keep"), ("Miami, FL", "Miami, Floride, États-Unis", "keep"),
    ("New York, NY", "Nueva York, Nueva York, Estados Unidos", "keep"), ("Hamburg, Germany", "Hamburg, Freie und Hansestadt Hamburg, Deutschland", "keep"),
    ("Vienna, Austria", "Wien, Österreich", "keep"), ("Mexico City, Mexico", "Ciudad de México, CDMX, México", "keep"),
    ("Austin, TX", "Toronto, Ontario, Kanada", "remove"), ("Seattle, WA", "Seattle, Washington, États-Unis", "keep"),
    # DC next to WA
    ("Washington, DC", "Seattle, WA", "remove"), ("Washington, DC", "Washington", "keep"), ("Washington, DC", "Washington, D.C., US", "keep"),
    ("Seattle, WA", "Washington, DC", "remove"), ("Seattle, Washington", "Washington, DC Metro Area", "remove"),
    ("Washington, DC", "Washington, DC Metro Area", "keep"), ("Washington, DC", "Spokane, Washington", "remove"),
    ("Washington, District of Columbia", "Arlington, VA", "remove"), ("Washington, DC", "Washington DC-Baltimore Area", "keep"),
    # only a state code, only a country
    ("San Francisco, California", "CA", "keep"), ("Indianapolis, Indiana", "IN", "keep"), ("Atlanta, Georgia", "GA", "keep"),
    ("Pittsburgh, Pennsylvania", "PA", "keep"), ("Austin, TX", "TX", "keep"), ("Austin, TX", "Canada", "remove"),
    ("London, United Kingdom", "United Kingdom", "keep"), ("Austin, TX", "Germany", "remove"), ("Berlin, Germany", "Deutschland", "keep"),
    ("Texas", "CO", "remove"), ("Texas", "Texas", "keep"),
    ("Pittsburgh, Pennsylvania", {"state": "PA", "country": "US"}, "keep"), ("Pittsburgh, Pennsylvania", {"region": "PA"}, "keep"),
    # empty, whitespace, numbers, null
    ("Austin, TX", "", "keep"), ("Austin, TX", "   ", "keep"), ("Austin, TX", None, "keep"), ("Austin, TX", 78701, "keep"),
    ("Austin, TX", ", ,", "keep"), ("Austin, TX", [], "keep"), ("Austin, TX", {"city": "Austin", "state": "TX"}, "keep"),
    ("Austin, TX", " | ; ", "keep"), ("   ", "Denver, CO", "keep"), (None, "Denver, CO", "keep"),
    # lists mixing states
    ("Austin, TX", ["Denver, CO", "Austin, TX"], "keep"), ("Austin, TX", ["Denver, CO", "Boise, ID"], "remove"),
    ("Austin, TX", "Denver, CO; Boise, ID | Reno, NV", "remove"), ("Austin, TX", "Denver, CO; Remote", "keep"),
    ("Portland, OR", ["Portland, ME", "Portland, OR"], "keep"), ("Chicago, IL", "Chicago, IL; Toronto, ON", "keep"),
    ("Toronto, Canada", "Buffalo, NY; Detroit, MI", "remove"), ("Austin, TX", ["Remote", "Dallas, TX"], "keep"),
]
KEEP_OR_REMOVE += [
    # full country names that are also US places
    ("Atlanta, Georgia", "Georgia", "keep"), ("Tbilisi, Georgia", "Georgia", "keep"), ("Georgia", "Batumi, Georgia", "keep"),
    ("Georgia", "Macon, GA", "keep"), ("Georgia", "Georgia, United States", "keep"), ("Savannah, Georgia", "Batumi, Georgia", "keep"),
    ("Jordan, MN", "Jordan, Minnesota", "keep"), ("Minneapolis, MN", "Jordan, MN", "keep"), ("Jordan, MN", "Jordan", "keep"),
    ("Amman, Jordan", "Jordan, MN", "remove"), ("Jordan", "Jordan, MN", "remove"),
    ("Lebanon, PA", "Lebanon, Pennsylvania", "keep"), ("Lebanon, PA", "Lebanon", "keep"), ("Beirut, Lebanon", "Lebanon, PA", "remove"),
    ("Lebanon, PA", "Lebanon, United States", "keep"),
    ("Peru, IN", "Peru, Indiana", "keep"), ("Peru, IN", "Peru", "keep"), ("Lima, Peru", "Peru, IN", "remove"),
    ("Peru, Indiana", "Peru, Indiana, United States", "keep"),
    ("Mexico, MO", "Mexico, Missouri", "keep"), ("Mexico, MO", "Mexico", "keep"), ("Mexico City, Mexico", "Mexico, MO", "remove"),
    ("Mexico", "Mexico, MO", "remove"), ("Mexico, MO", "Albuquerque, New Mexico", "remove"), ("Mexico", "Albuquerque, New Mexico", "remove"),
    ("Panama City, FL", "Panama City, Florida", "keep"), ("Panama City, FL", "Panama City", "keep"), ("Panama City, FL", "Panama City, Panama", "remove"),
    ("Panama City, Panama", "Panama City, FL", "remove"), ("Panama City, FL", "Panama City Beach, FL", "keep"),
    ("Paris, TX", "Paris, Texas", "keep"), ("Paris, TX", "Paris", "keep"), ("Paris, TX", "Paris, France", "remove"), ("Lyon, France", "Paris, TX", "remove"),
    ("Paris, France", "Paris, Texas, United States", "remove"),
    ("Jersey City, NJ", "Jersey City", "keep"), ("Brazil, IN", "Brazil", "keep"), ("Norway, ME", "Norway", "keep"),
    ("Holland, MI", "Holland, Michigan", "keep"), ("Amsterdam, Netherlands", "Holland, MI", "remove"),
    # full US state names used abroad
    ("Melbourne, Victoria", "Melbourne, VIC, Australia", "keep"), ("Victoria, BC", "Victoria, British Columbia, Canada", "keep"),
    ("Victoria, Canada", "Victoria, BC", "keep"), ("Victoria", "Victoria, Australia", "keep"), ("Melbourne, Australia", "Melbourne, Victoria", "keep"),
    ("Victoria, Australia", "Victoria, TX", "remove"), ("Melbourne, Australia", "Melbourne, Florida", "remove"),
    ("Washington, Tyne and Wear, UK", "Washington, England", "keep"), ("Washington, Tyne and Wear, UK", "Sunderland, UK", "keep"),
    ("Washington, Tyne and Wear, UK", "Washington", "keep"), ("Sunderland, UK", "Washington, Tyne and Wear, UK", "keep"),
    ("Seattle, WA", "Washington, Tyne and Wear, UK", "remove"), ("Seattle, Washington", "Washington, UK", "remove"),
    ("Indiana, Pennsylvania", "Indiana, PA", "keep"), ("Pittsburgh, PA", "Indiana, Pennsylvania", "keep"),
    ("Indianapolis, Indiana", "Indiana, PA", "remove"), ("Indiana, PA", "Indianapolis, IN", "remove"), ("Indiana", "Indiana, Pennsylvania", "remove"),
    # country names inside city names
    ("Kuwait City, Kuwait", "Kuwait City", "keep"), ("Kuwait", "Kuwait City, Kuwait", "keep"), ("Kuwait City, Kuwait", "Kuwait", "keep"),
    ("Kuwait City", "Kuwait City, KW", "keep"), ("Austin, TX", "Kuwait City", "remove"),
    ("Mexico City, Mexico", "Mexico City", "keep"), ("Mexico City", "Mexico City, CDMX", "keep"), ("Mexico City, Mexico", "Mexico City, Mexico City, Mexico", "keep"),
    ("Guatemala City, Guatemala", "Guatemala City", "keep"), ("Guatemala City", "Guatemala", "keep"), ("Guatemala City, Guatemala", "Guatemala City, GT", "keep"),
    ("Luxembourg City", "Luxembourg", "keep"), ("Luxembourg", "Luxembourg City, Luxembourg", "keep"),
    ("Panama City, Panama", "Panama City", "keep"), ("Panama", "Panama City", "keep"),
    # near-identical country names
    ("Dublin, Ireland", "Belfast, Northern Ireland", "remove"), ("Belfast, Northern Ireland", "Belfast, UK", "keep"),
    ("Belfast, UK", "Belfast, Northern Ireland", "keep"), ("Dublin, Ireland", "Dublin, Republic of Ireland", "keep"),
    ("Ireland", "Northern Ireland", "remove"), ("Northern Ireland", "Ireland", "remove"),
    ("Brazzaville, Congo", "Kinshasa, DR Congo", "remove"), ("Kinshasa, DR Congo", "Kinshasa, Democratic Republic of the Congo", "keep"),
    ("Kinshasa, Congo", "Kinshasa, DR Congo", "keep"), ("Kinshasa, Congo", "Kinshasa, Democratic Republic of the Congo", "keep"),
    ("Seoul, Korea", "Seoul, South Korea", "keep"), ("Korea", "Busan, Republic of Korea", "keep"), ("Seoul, Korea", "Seoul, KR", "keep"),
    ("Seoul, South Korea", "Pyongyang, North Korea", "remove"), ("Seoul, South Korea", "Seoul, Korea", "keep"),
    ("Niamey, Niger", "Lagos, Nigeria", "remove"), ("Nigeria", "Niger", "remove"), ("Lagos, Nigeria", "Lagos, NG", "keep"), ("Niger", "Niamey, NE", "keep"),
    ("Roseau, Dominica", "Santo Domingo, Dominican Republic", "remove"), ("Santo Domingo, Dominican Republic", "Santo Domingo, DR", "keep"),
    ("Dominican Republic", "Dominica", "remove"),
    ("Conakry, Guinea", "Bissau, Guinea-Bissau", "remove"), ("Port Moresby, Papua New Guinea", "Port Moresby, PNG", "keep"),
    ("Guinea", "Port Moresby, Papua New Guinea", "remove"), ("Bissau, Guinea-Bissau", "Bissau, Guinea Bissau", "keep"),
    ("Khartoum, Sudan", "Juba, South Sudan", "remove"), ("South Sudan", "Juba", "keep"), ("Juba, South Sudan", "Juba, Sudan", "remove"),
    # US territories
    ("San Juan, Puerto Rico", "San Juan, PR, United States", "keep"), ("San Juan, Puerto Rico", "San Juan, Puerto Rico, United States", "keep"),
    ("Puerto Rico", "San Juan, PR", "keep"), ("Austin, TX", "San Juan, Puerto Rico", "remove"), ("United States", "San Juan, Puerto Rico", "keep"),
    ("San Juan, PR", "San Juan, Puerto Rico", "keep"), ("San Juan, PR", "San Juan, United States", "keep"),
    ("Charlotte Amalie, US Virgin Islands", "Charlotte Amalie, VI, United States", "keep"), ("United States", "St. Thomas, US Virgin Islands", "keep"),
    ("US Virgin Islands", "St. Thomas, USVI", "keep"), ("Charlotte Amalie, US Virgin Islands", "Charlotte Amalie, Virgin Islands, U.S.", "keep"),
    # UK nations, other names for one country
    ("London, England", "London, UK", "keep"), ("Edinburgh, UK", "Edinburgh, Scotland", "keep"), ("Cardiff, Wales", "Cardiff, United Kingdom", "keep"),
    ("England", "Glasgow, Scotland", "remove"), ("Wales", "Cardiff, Great Britain", "keep"), ("Scotland", "Glasgow, GB", "keep"),
    ("Amsterdam, Netherlands", "Amsterdam, Holland", "keep"), ("Holland", "Rotterdam, Netherlands", "keep"), ("Rotterdam, Holland", "Rotterdam, Netherlands", "keep"),
    ("Prague, Czechia", "Prague, Czech Republic", "keep"), ("Czech Republic", "Brno, Czechia", "keep"),
    ("Istanbul, Türkiye", "Istanbul, Turkey", "keep"), ("Turkey", "Ankara, Türkiye", "keep"), ("Istanbul, Turkey", "Istanbul, Turkiye", "keep"),
    ("Abidjan, Ivory Coast", "Abidjan, Côte d'Ivoire", "keep"), ("Côte d'Ivoire", "Abidjan, Ivory Coast", "keep"), ("Ivory Coast", "Abidjan, Cote d'Ivoire", "keep"),
]
# Clear to a reader, kept by the test: another city in the same state (plain text cannot tell
# it from a borough, a county or an address), a city asked with nothing around it, a place that
# may be a US state or a country ('IN', 'MN', 'Georgia'), a country in another language
# ('Kanada'), a two-letter code in a row for an ask abroad, which never removes, the two Congos,
# a country inside another (Guinea, Guinea-Bissau; England, Scotland), and a one-part row that
# names the asked place's first part ('Jordan' as a town).
NOT_JUDGED = {
    ("Berlin", "Toronto, Canada"), ("India", "Indianapolis, IN"), ("Mongolia", "Minneapolis, MN"),
    ("Atlanta, Georgia", "Savannah, Georgia"), ("Atlanta, GA", "Tbilisi, Georgia"), ("Austin, TX", "Dallas, TX"),
    ("Oklahoma City, OK", "Tulsa, Oklahoma"), ("New York, NY", "Buffalo, NY"), ("Austin, Texas, United States", "Dallas, TX"),
    ("Dallas, TX", "Fort Worth, Texas"), ("Cambridge, MA", "Somerville, Massachusetts"),
    ("San Diego, California", "Sacramento, CA"), ("Phoenix, AZ", "Tucson, Arizona, United States"),
    ("Austin, TX", "Houston, TX 77002"), ("austin, tx", "DALLAS, TEXAS"), ("Denver, Colorado", "Boulder, CO"),
    ("Dover, DE", "Wilmington, Delaware"), ("Atlanta, GA", "Kutaisi, Georgia"), ("Boston, Massachusetts", "Worcester, MA"),
    ("Podgorica, ME", "Portland, Maine"), ("Ulaanbaatar, MN", "Minneapolis, Minnesota"),
    ("Canada", "Seattle, WA"), ("Paris, France", "Paris, TX"), ("Lima, Peru", "Lima, OH"),
    ("Manchester, UK", "Manchester, NH"), ("Austin, TX", "Toronto, Ontario, Kanada"),
    ("Toronto, Canada", "Buffalo, NY; Detroit, MI"),
    ("Amman, Jordan", "Jordan, MN"), ("Jordan", "Jordan, MN"), ("Beirut, Lebanon", "Lebanon, PA"),
    ("Lima, Peru", "Peru, IN"), ("Mexico City, Mexico", "Mexico, MO"), ("Mexico", "Mexico, MO"),
    ("Panama City, Panama", "Panama City, FL"), ("Lyon, France", "Paris, TX"), ("Amsterdam, Netherlands", "Holland, MI"),
    ("Victoria, Australia", "Victoria, TX"), ("Indiana, PA", "Indianapolis, IN"), ("Brazzaville, Congo", "Kinshasa, DR Congo"),
    ("Conakry, Guinea", "Bissau, Guinea-Bissau"), ("England", "Glasgow, Scotland"),
}


@pytest.mark.parametrize("asked,row,want", KEEP_OR_REMOVE)
def test_place_never_removes_a_row_in_the_place_asked(asked, row, want):
    got = R.place(asked, row)
    if isinstance(row, str) and (asked, row) in NOT_JUDGED:
        assert got is not False
    else:
        assert (got is False) == (want == "remove"), got


@pytest.mark.parametrize("name", ["Washington, DC", "Kansas City, MO", "Nevada City, CA", "Indiana, PA",
                                  "Delaware, OH", "Michigan City, IN", "New York City", "Texas"])
def test_a_place_always_matches_itself(name):
    assert R.place(name, name) is True


def test_an_ask_that_reads_two_ways_removes_a_row_only_when_both_readings_do():
    assert R.place("San Francisco, CA", "Paris, France") is False
    assert R.place("San Francisco, CA", "Los Angeles, CA") is None      # California, or Canada
    assert R.place("San Francisco, CA", "San Francisco, CA, US") is True
    assert R.place("Mumbai, IN", "Mumbai, Maharashtra, India") is True
    assert R.place("Toronto, CA", "Toronto, Ontario, Canada") is True
    assert R.place("Tbilisi, GE", "Tbilisi, Georgia") is True and R.place("Georgia", "Tbilisi, GE") is not False
    assert R.place("Oklahoma City, OK", "Tulsa, Oklahoma") is None       # another city part: never judged


def test_a_row_naming_several_places_is_removed_only_when_every_one_is_elsewhere():
    assert R.place("San Francisco, CA", "New York, NY (HQ); San Francisco, CA") is True
    assert R.place("Austin, TX", ["Berlin, Germany", "Remote"]) is None
    assert R.place("Austin, TX", "Berlin, Germany | Paris, France") is False


def test_a_two_letter_code_removes_a_row_only_for_an_ask_in_the_us():
    assert R.place("Texas", "Perth, WA") is False and R.place("Perth, Australia", "Perth, WA") is True
    assert R.place("Australia", "Adelaide, SA") is None and R.place("Germany", "Munich, BY") is None
    assert R.place("Austin, TX", "London, UK") is False                  # UK is a name, not a code
    assert R.place("Seattle, Washington", "Washington, DC Metro Area") is False
    assert R.place("Washington, DC", "Washington") is True               # the asked place's own first part
    assert R.place("Texas", "Washington") is False and R.place("District of Columbia", "Washington") is None
    assert R.place("Canada", "CA") is None and R.place("California", "CA") is True


def test_territories_and_one_part_rows():
    assert R.place("United States", "San Juan, Puerto Rico") is not False
    assert R.place("Austin, TX", "San Juan, Puerto Rico") is False      # a territory is in no state
    assert R.place("Lebanon, PA", "Lebanon") is True and R.place("Panama City, FL", "Panama City") is True
    assert R.place("Washington, Tyne and Wear, UK", "Washington") is True


def test_a_two_letter_region_before_the_country_is_no_country():
    assert R.place("Canada", "Saskatoon, SK, Canada") is True
    assert R.place("Netherlands", "St. John's, NL, Canada") is False
    assert R.place("United Kingdom", "London, UK") is True


def test_place_counts_rows_removed_and_unchecked():
    rows = [{"location": "Austin, TX, US"}, {"location": "Dallas, Texas, United States"},
            {"location": "Greater Austin Area"}, {"location": "Berlin, Germany"}, {"location": "Texas"},
            {"name": "no place"}]
    r = _apply("location", "place", ["location"], "Austin, Texas", rows)
    assert [x.get("location") for x in r.rows] == ["Austin, TX, US", "Dallas, Texas, United States",
                                                   "Greater Austin Area", "Texas", None]
    assert r.removed == 1 and r.unchecked == 3 and r.by == {"location": 1}


def test_domain_keeps_the_company_and_its_subdomains():
    rows = [{"company_domain": "stripe.com"}, {"company_domain": "jobs.stripe.com"}, {"company_domain": "stripe.com."},
            {"company_domain": "user@stripe.com"},
            {"company_domain": "stripe.com:443"}, {"company_domain": "notstripe.com"}, {"company_domain": "acme.io"}]
    r = _apply("company_domain", "domain", ["company_domain"], "https://www.stripe.com", rows)
    assert [x["company_domain"] for x in r.rows] == ["stripe.com", "jobs.stripe.com", "stripe.com.", "user@stripe.com",
                                                     "stripe.com:443"]
    assert r.removed == 2


def test_site_keeps_rows_on_the_questions_site():
    rows = [{"link": "https://www.a.com/blog/post"}, {"url": "https://a.com/shop"}, {"url": "https://b.com/blog"}]
    r = _apply("q", "site", ["url", "link"], "pricing site:a.com/blog", rows)
    assert r.rows == rows[:1] and r.removed == 2
    # a question without `site:` is not judged at all
    assert _apply("q", "site", ["url", "link"], "pricing", rows) is None


@pytest.mark.parametrize("q,link", [
    ("x site:.edu", "https://mit.edu/a"), ("x site:*.a.com", "https://b.com/a"), ("x site:edu", "https://b.com"),
    ("x site:a.com", "https://www.google.com/url?q=https://a.com/p"), ("x site:a.com/caf%C3%A9", "https://a.com/x"),
    ("x site:a.com", "https://b.com/%2Fa.com"), ("x site:a.com", "not a link"),
])
def test_site_does_not_judge_what_it_cannot_read(q, link):
    assert R.site(q, link) is None


def test_within_days_keeps_rows_posted_in_the_window():
    rows = [{"date_posted": "2026-11-25T10:00:00+00:00"}, {"date_first_seen": "2026-11-21"},
            {"date_posted": "2026-10-01"}, {"date_posted": "last week"}]
    r = _apply("posted_within_days", "within_days", ["date_posted", "date_first_seen"], 7, rows)
    assert r.rows == [rows[0], rows[1], rows[3]] and r.removed == 1 and r.unchecked == 1


def test_within_days_reads_the_row_date_in_utc():
    # 20 November 22:00 at UTC-5 is 21 November in UTC: inside a 7-day window from 28 November
    assert R.within_days(7, "2026-11-20T22:00:00-05:00", TODAY) is True
    assert R.within_days(7, "2026-11-21T02:00:00+05:00", TODAY) is False
    assert R.within_days(7, "2026-11-21T00:00:00Z", TODAY) is True


def test_a_filter_the_caller_did_not_send_judges_nothing():
    f = (R.RowFilter("location", "place", ("location",)),)
    assert R.apply(f, {"location": "Austin"}, set(), [{"location": "Berlin"}], TODAY) is None


def test_row_filters_refuse_a_key_or_field_the_contract_does_not_have():
    base = {"identity": [{"q": "string"}], "filters": {"location": {"type": "string"}},
            "output": {"people": {"type": "array", "required": True}}, "miss": "people == []",
            "row": {"of": "people", "needs": ["name"], "fields": {"name": {"type": "string"}, "location": {"type": "string"}}}}
    ok = parse_contracts({"contracts": {"x": {**base, "row_filters": {"location": {"test": "place", "field": "location"}}}}})
    assert ok["x"].row_filters == (R.RowFilter("location", "place", ("location",)),)
    for bad in ({"country": {"test": "place", "field": "location"}}, {"location": {"test": "near", "field": "location"}},
                {"location": {"test": "place", "field": "city"}}):
        with pytest.raises(ValueError, match="row_filters"):
            parse_contracts({"contracts": {"x": {**base, "row_filters": bad}}})


def test_shipped_contracts_declare_their_row_filters():
    contracts = catalog_store.load().contracts
    assert {f.key for f in contracts["people.search"].row_filters} == {"location", "company_domain"}
    assert {f.key for f in contracts["companies.jobs.search"].row_filters} == {"location", "posted_within_days"}
    assert {f.key for f in contracts["web.search"].row_filters} == {"q"}


def test_the_loader_refuses_a_field_no_providers_row_carries():
    from dataclasses import replace

    from treg.domain.catalog.routing.contracts import row_filter_fields_unread
    cat = catalog_store.load()

    def example(ep):
        path = catalog_store.example_path(ep["id"])
        return json.loads(path.read_text()) if path else ep.get("example_response")

    assert row_filter_fields_unread(cat.contracts, cat.adapters, cat.by_id, example) == []
    jobs = cat.contracts["companies.jobs.search"]
    wrong = {**cat.contracts, "companies.jobs.search": replace(
        jobs, row_filters=(R.RowFilter("posted_within_days", "within_days", ("posted_on",)),))}
    assert row_filter_fields_unread(wrong, cat.adapters, cat.by_id, example) == [
        "companies.jobs.search.posted_within_days: posted_on"]


# ---- through the routed call ------------------------------------------------------------------

LEADS = {"leads": [
    {"firstname": "Ann", "lastname": "A", "lastCompanyWebsite": "https://stripe.com", "address": "Dublin, Ireland", "profileUrl": "https://www.linkedin.com/in/ann"},
    {"firstname": "Bob", "lastname": "B", "lastCompanyWebsite": "https://acme.io", "address": "Dublin, Ireland", "profileUrl": "https://www.linkedin.com/in/bob"},
    {"firstname": "Cat", "lastname": "C", "lastCompanyWebsite": "https://stripe.com", "address": "London, United Kingdom", "profileUrl": "https://www.linkedin.com/in/cat"},
    {"firstname": "Dan", "lastname": "D", "lastCompanyWebsite": "https://stripe.com", "profileUrl": "https://www.linkedin.com/in/dan"},
], "total": 4}
FAR = {"leads": [{"firstname": "Eve", "lastname": "E", "lastCompanyWebsite": "https://acme.io", "address": "Berlin, Germany",
                  "profileUrl": "https://www.linkedin.com/in/eve"}], "total": 1}
ASK = {"company_domain": "stripe.com", "title": "Engineer", "location": "Dublin, Ireland", "limit": 4}


@pytest.fixture
def icypeas_only(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas")
    get_settings.cache_clear()
    cat = catalog_store.load()
    for eid in cat.by_id["treg.people.search"]["routed_children"]:
        if eid != "icypeas.people.search":
            monkeypatch.delitem(cat.adapters, eid)
    yield
    get_settings.cache_clear()


async def _search(clients, monkeypatch, answer, *, strict: bool):
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, answer)]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json=ASK,
                           headers={"X-Treg-Route-Strict-Filters": "1"} if strict else {})
    assert r.status_code == 200 and len(seen) == 1, r.text
    return r, before - await _balance(clients)


async def test_strict_filters_remove_rows_off_place_or_off_company_and_bill_as_usual(clients: AsyncClient, icypeas_only, monkeypatch):
    plain, plain_cost = await _search(clients, monkeypatch, LEADS, strict=False)
    strict, strict_cost = await _search(clients, monkeypatch, LEADS, strict=True)
    # a call without the header is unchanged: every row, no note
    assert [p["name"] for p in plain.json()["output"]["people"]] == ["Ann A", "Bob B", "Cat C", "Dan D"]
    assert "strict_filtered" not in plain.json()["_treg"] and "X-Treg-Strict-Filtered" not in plain.headers
    body = strict.json()
    # Bob works elsewhere, Cat is in London; Dan names no place, so he is kept, unchecked
    assert [p["name"] for p in body["output"]["people"]] == ["Ann A", "Dan D"]
    assert body["_treg"]["strict_filtered"] == {"removed": 2, "kept": 2, "by": {"company_domain": 1, "location": 1},
                                               "unchecked": 1, "in_raw": True}
    assert strict.headers["X-Treg-Strict-Filtered"] == "removed=2,kept=2"
    assert body["raw"] == LEADS and body["_treg"]["outcome"] == "hit"   # the child's answer, whole
    assert strict_cost == plain_cost > 0 and body["_treg"]["charged_micro"] == plain.json()["_treg"]["charged_micro"]


async def test_strict_filters_that_remove_every_row_answer_a_billed_miss_with_raw_whole(clients: AsyncClient, icypeas_only, monkeypatch):
    plain, plain_cost = await _search(clients, monkeypatch, FAR, strict=False)
    strict, strict_cost = await _search(clients, monkeypatch, FAR, strict=True)
    body = strict.json()
    assert body["output"]["people"] == [] and body["raw"] == FAR
    assert body["_treg"]["outcome"] == "miss" and strict.headers["X-Treg-Route-Outcome"] == "miss"
    assert body["_treg"]["strict_filtered"]["removed"] == 1 and body["_treg"]["strict_filtered"]["kept"] == 0
    assert "strict filters removed every row" in body["_treg"]["tried"][0]["detail"]
    # the provider answered: its call settles at its own price, as without the header
    assert strict_cost == plain_cost > 0 and body["_treg"]["charged_micro"] == plain.json()["_treg"]["charged_micro"]


OFF_COMPANY = {
    "leads": [{"firstname": "Eve", "lastname": "E", "firstName": "Eve", "lastName": "E", "lastCompanyWebsite": "https://acme.io",
               "company": {"domain": "acme.io"}, "profileUrl": "https://www.linkedin.com/in/eve"}],
    "people": [{"contact_full_name": "Eve E", "company_domain": "acme.io"}],
    "data": [{"first_name": "Eve", "last_name": "E", "company_url": "https://acme.io"}],
    "contacts": [{"name": "Eve E", "domain": "acme.io", "email": "eve@acme.io"}],
    "total": 1, "count": 1,
}
COMPANY_SEARCHES = ("icypeas.people.search", "leadsforge.people.search", "leadmagic.people.search",
                    "quickenrich.people.search.domain", "findymail.search.domain")


@pytest.fixture
def five_company_searches(monkeypatch, platform_on):
    for p in ("ICYPEAS", "LEADSFORGE", "LEADMAGIC", "QUICKENRICH", "FINDYMAIL"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas,leadsforge,leadmagic,quickenrich,findymail")
    get_settings.cache_clear()
    cat = catalog_store.load()
    for eid in cat.by_id["treg.people.search"]["routed_children"]:
        if eid not in COMPANY_SEARCHES:
            monkeypatch.delitem(cat.adapters, eid)
    yield
    get_settings.cache_clear()


async def test_strict_filters_ask_at_most_two_more_providers_after_an_emptied_answer(
        clients: AsyncClient, five_company_searches, monkeypatch):
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, OFF_COMPANY)] * 5}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": 1},
                           headers={"X-Treg-Route-Strict-Filters": "1"})
    body = r.json()
    assert r.status_code == 200 and body["_treg"]["outcome"] == "miss", r.text
    asked = [t for t in body["_treg"]["tried"] if t["outcome"] != "skipped"]
    # the first answer and at most two more, each billed at its own price
    assert len(seen) == 3 and len(asked) == 3, body["_treg"]["tried"]
    assert all("strict filters removed every row" in t["detail"] for t in asked)
    assert body["_treg"]["charged_micro"] == sum(t["charged_micro"] for t in asked) == before - await _balance(clients)
    assert body["output"]["people"] == [] and body["raw"] == OFF_COMPANY
    # without the header the first provider's rows are the answer
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, OFF_COMPANY)]}, seen))
    r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": 1})
    assert r.status_code == 200 and len(seen) == 1 and r.json()["_treg"]["outcome"] == "hit", r.text


async def test_weak_and_emptied_answers_share_one_bound(clients: AsyncClient, five_company_searches, monkeypatch):
    on = json.loads(json.dumps(OFF_COMPANY).replace("acme.io", "stripe.com"))
    seen: list = []
    # one row each where two make an answer (weak), alternating with answers strict filtering empties
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"*": [(200, on), (200, OFF_COMPANY), (200, on), (200, OFF_COMPANY), (200, on)]}, seen))
    r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": 2},
                           headers={"X-Treg-Route-Strict-Filters": "1", "X-Treg-Route-Min-Results": "2"})
    body = r.json()
    assert r.status_code == 200 and len(seen) == 3, body["_treg"]["tried"]
    assert body["_treg"]["outcome"] == "weak" and len(body["output"]["people"]) == 1


async def test_a_strict_emptied_next_page_still_carries_the_cursor(clients: AsyncClient, five_company_searches, monkeypatch):
    page1 = {**json.loads(json.dumps(OFF_COMPANY).replace("acme.io", "stripe.com")), "cursor": "p2"}
    page2 = {**OFF_COMPANY, "cursor": "p3"}
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, page1), (200, page2)]}, seen))
    ask = {"company_domain": "stripe.com", "limit": 1}
    headers = {"X-Treg-Route-Strict-Filters": "1", "X-Treg-Route-Prefer": "leadsforge"}
    first = await clients.post("/call/treg.people.search", json=ask, headers=headers)
    assert first.status_code == 200 and first.json()["_treg"]["served_by"] == "leadsforge.people.search", first.text
    cursor = first.json()["output"]["next_cursor"]
    assert cursor
    r = await clients.post("/call/treg.people.search", json={**ask, "cursor": cursor}, headers=headers)
    body = r.json()
    # every row of page 2 is at another company: the page is a miss, and the list still goes on
    assert r.status_code == 200 and body["_treg"]["outcome"] == "miss" and body["output"]["people"] == [], r.text
    assert body["output"]["next_cursor"] and body["output"]["next_cursor"] != cursor
    assert len(seen) == 2 and seen[1][2].get("cursor") == "p2"
