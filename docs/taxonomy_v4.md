# Mosferics Taxonomy — v4 (freeze candidate)

**Supersedes `taxonomy_v3.md` (v3.1).** Adds a full **Food & Beverage** department (`FNB`), reserved at level 1 so it survives the SKU freeze, and moves all consumable food/drink out of the equipment tree. Everything else carries forward from v3.1 (which applied `Taxonomy issues v2.rtf` and `taxonomy corrections.rtf`).

Built to serve `mosferics_data_model_v2.md`: nodes map to `catalog.taxonomy`, `link.category_map` targets them, and **level 1 is the frozen layer** whose prefixes become immutable `MOS-<DEPT>-<NNNNNNN>` SKU codes. **12 departments.**

### v4 changes

- **New department 12 — Food & Beverage `FNB`**, reserved for the full Uropa Food & Drink range (soft drinks, hot beverages, sauces, spices, snacks) as well as bar ingredients. Level-2 targets until Uropa's categories arrive.
- **Bar ingredients moved out of Bar → FnB.** The provisional `Cocktail Ingredients & Mixers` category is gone from Dept 4; syrups, purées, cordials, cocktail mixers and bitters now live in FnB. Bar keeps the *equipment* (glass rimmers, empty dash/bitter bottles).
- **Food garnishes moved to FnB.** Rimming sugars & salts, dried-fruit garnishes, and cocktail cherries/onions/olives are food → FnB `Cocktail Garnishes & Rimmers`. Cocktail *sticks/picks* (empty) stay in DIS.
- **DIS ↔ FnB boundary rule (the packaged-food-to-go collision):**
  > **If food touches the product, it is FnB. If it is an empty vessel, it is DIS.**
  Verified against the live catalogue: every "Hot Food Packaging", "Cold Food Packaging" and "Food & Drink To Go" item is an *empty* container (Notpla boxes, PLA portion pots, kraft bowls, lids, platter boxes) → **stays in DIS**. Pre-filled / food-containing grab-and-go items → **FnB `Packaged Food to Go`**.

---

## Rules (v4)

1. Three levels max: **Department → Category → Type**. Sparse branches stop at Category (a level-2 node is a valid `category_map` target).
2. **12 departments** at level 1 — the frozen layer. Prefixes below.
3. Every level-3 leaf ≥ ~5 products *or* a genuine search term. Sparse types roll up.
4. **One canonical node per product.** No dual homes — `category_map` must be deterministic or pricing-rule scoping and template selection drift.
5. Cross-cutting attributes are **filters/metadata, not branches**: brand/range, material, colour, safe-for flags, compliance. No exceptions.
6. Merchandising & lifecycle are **collections**: Best Seller, Seasons, Clearance, Festival & Outdoor, venue opening lists, **Bar Essentials** (cross-sells barware with FnB mixers without branching the tree).
7. **Variant-axis convention:** primary axis = **Option 1**, fixed per node, never varies; max 3 (Shopify ceiling), keep headroom.
8. **Consumable vs vessel:** food/drink that is consumed or that contains food → **FnB**; the empty thing it is served/packed in → its equipment/disposables department.

**Sanity tests (≤3 clicks):** 12oz hi-ball → Glassware → Tumblers & Hi-Balls → Hi-Ball & Tall Tumblers ✅ · grenadine syrup → Food & Beverage → Bar Syrups, Purées & Mixers → Syrups ✅ · empty burger box → Disposables → Hot Food Packaging ✅

---

## Level 1 — frozen departments and SKU prefixes

`MOS-<DEPT>-<NNNNNNN>`, single global sequence, prefix stored immutable on the department node.

| # | Department | Prefix | Uropa slot? |
|---|---|---|---|
| 1 | Glassware & Drinkware | `GLA` | |
| 2 | Tableware & Crockery | `TAB` | |
| 3 | Cutlery & Flatware | `CUT` | |
| 4 | Bar & Cocktail | `BAR` | |
| 5 | Kitchen & Cookware | `KIT` | |
| 6 | Food Storage & Gastronorm | `GAS` | |
| 7 | Catering Equipment & Appliances | `EQP` | **reserved** |
| 8 | Disposables & Food-to-Go | `DIS` | |
| 9 | Cleaning, Hygiene & Safety | `HYG` | partial |
| 10 | Clothing & Workwear (PPE) | `CLO` | **reserved** |
| 11 | Furniture, Fixtures & Display | `FUR` | **reserved** |
| 12 | Food & Beverage | `FNB` | **reserved** |

---

## The tree

*Format: **Category** `[variant_axes in option order]` → leaf types. `[]` = no variant axis (usually one variant per product; pack size is a field, not an axis).*

### 1. Glassware & Drinkware `GLA` — *cold-drink vessels only*
*Material (glass / crystal / polycarbonate / rPET / toughened) is a filter. Festival & Outdoor is a collection. Hot-drink vessels (glass or ceramic) live in Dept 2.*

- **Beer Glasses** `[capacity, colour]` → Pint · Half-Pint · Stemmed Beer · Mugs, Steins & Tankards
- **Wine Glasses** `[capacity, colour]` → Red Wine · White Wine · Universal & Water
- **Champagne & Sparkling** `[capacity, colour]` → Flutes · Coupes & Champagne Saucers
- **Tumblers & Hi-Balls** `[capacity, colour]` → Hi-Ball & Tall Tumblers · Old Fashioned, Rocks & Whisky · Water & Juice Tumblers
- **Spirit & Cocktail Glasses** `[capacity, colour]` → Martini · Coupette & Nick & Nora · Margarita · Gin & Balloon · Hurricane & Sling · Shot Glasses *(punchbowls/fishbowls, 4 products, roll up here)*
- **Jugs, Carafes & Decanters** `[capacity, colour]`

### 2. Tableware & Crockery `TAB`
*Brand ranges (Seasons, Aura, Porcelite, Steelite) and material are metadata.*

- **Plates** `[size, colour]` → Round & Classic · Square & Rectangular · Pasta, Pizza & Gourmet · Side & Presentation
- **Bowls** `[size, colour]` → Serving & Salad · Pasta & Soup · Ramekins & Small Dishes
- **Cups, Mugs & Saucers** `[capacity, colour]` → Cups & Saucers · Mugs · Espresso & Cappuccino Cups · Tea & Coffee Glasses *(hot-drink vessels, glass & ceramic together)*
- **Teapots & Beverageware** `[capacity, colour]` → Teapots & Coffee Pots · Milk & Frothing Jugs · Tea & Coffeeware *(the vessels/equipment you brew into or with; the tea/coffee itself → Dept 12)*
- **Dessert & Sundae Serveware** `[]` → all dessert/sundae dishes, any material *(single home)*
- **Serveware & Presentation** `[]` → Platters & Sauce Boats · Steel Presentation (stainless / vintage / copper / hammered / galvanised) · Wood Presentation · Tapas
- **Table Dressing & Service** `[]` → Candles & Holders · Table Caddies · Condiment Holders, Dispensers, Salt & Pepper *(the empty holders — the seasoning inside → FnB)* · Napkins (linen & paper) · Tip Trays & Bill Presenters

### 3. Cutlery & Flatware `CUT`
*Grade (18/0, 18/10, 13/0) and pattern/range are metadata.*

- **Table Cutlery** `[finish]` → Table Knives · Table Forks · Table & Soup Spoons
- **Dessert & Tea Cutlery** `[finish]` → Dessert Cutlery · Teaspoons & Coffee Spoons
- **Serving & Speciality Cutlery** `[finish]` → Serving Spoons & Servers · Steak Knives & Forks · Cake Slices
- **Cutlery Storage** `[]` → Cutlery Trays & Holders

### 4. Bar & Cocktail `BAR` — *equipment only; ingredients live in Dept 12*
- **Measures & Pourers** `[measure_size]` → Spirit Measures (CA thimble / stamped) · Non-CA Jiggers · Measure Dispensers & Brackets · Speed Pourers · Dash & Bitter Bottles *(empty bottles = equipment; bitters the liquid → Dept 12)*
- **Cocktail Making** `[]` → Cocktail Shakers · Strainers · Mixing Glasses · Bar & Cocktail Spoons · Muddlers · Mexican Elbows · Glass Rimmers *(the tool; rimming sugars/salts → Dept 12)*
- **Ice, Garnish & Bar Tools** `[]` → Garnish Tweezers & Tools *(reusable)* · Bar Knives · Ice Buckets & Tools · Ice Crushers *(manual)* · Wine Coolers & Stands
- **Bar Sundries & Fixtures** `[colour]` → Bar Mats & Shelf Liners · Speed Rails · Bar Caddies · Drip Trays · Glass Hangers · Bar Trays & Carriers · Recycling Bins & Bottle Skips · Cellar & Cask *(fold-in; splits out on real range)* · Tiki & Metal Mugs *(ceramic + metal, material filter)*

### 5. Kitchen & Cookware `KIT`
- **Cookware** `[size]` → Pots & Pans · Frying & Sauté Pans · Woks · Casserole & Stew Pans · Bain Marie Pots & Lids
- **Bakeware** `[size]` → Baking Trays, Sheets & Tins · Pizza Pans & Utensils · Cake, Pastry & Cupcake · Cooling Racks
- **Knives & Sharpening** `[]` → Chefs Knives · Paring, Boning, Filleting & Carving · Bread & Serrated · Santoku & Utility · Knife Sets, Blocks & Wallets · Honing Steels & Whetstones · Kitchen Scissors
- **Kitchen Utensils** `[colour]` → Tongs · Spatulas & Turners · Cooking & Serving Spoons · Ladles & Spoodles · Whisks & Paddles · Spiders & Skimmers · Lifters & Slicers · Mashers & Ricers · Oven Gloves & Cloths
- **Prep Tools & Gadgets** `[colour]` → Chopping Boards & Accessories · Peelers, Zesters & Graters · Mandolines & Slicers · Can Openers · Squeeze & Sauce Dispensers *(the empty bottle; sauce inside → FnB)* · Dredgers & Shakers · Funnels, Sieves & Filters · Citrus Juicers · Pestle & Mortar · Blow Torches
- **Measuring & Thermometers** `[]` → Measuring Jugs, Cups & Spoons · Measuring Containers · Thermometers & Timers · Scales

### 6. Food Storage & Gastronorm `GAS`
*`colour` (colour-coded) is a filter, not an axis — keeps Option 3 free.*

- **Gastronorm Pans & Lids** `[gn_size, depth]` → Stainless GN · Polycarbonate GN · Polypropylene GN · GN Liners & Covers · GN Trolleys & Racks
- **Storage Containers** `[size]` → Food Storage Containers · Flexi Tubs · Ingredient Bins · Handled & Preserving Jars · Ice Cream Containers
- **Prep & Labelling** `[]` → Pizza Dough Boxes · Preserving Bags & Bottles · Storage, Allergen & Use-by Labels · Transport & Delivery Bags

### 7. Catering Equipment & Appliances `EQP` — *level 2 only until Uropa*
- **Cooking Appliances** `[]` — fryers, combi ovens, contact grills, water boilers, bain maries, rice cookers, dehydrators
- **Refrigeration** `[]` — beer fridges, bottle coolers, ice machines
- **Bar & Beverage Machines** `[]` — bar blenders, cereal & drink dispensers
- **Warewashing** `[]` — glasswashers, dishwasher baskets & racks, water filters
- **Spare Parts** `[]` — Polar / Buffalo spares & accessories

### 8. Disposables & Food-to-Go `DIS` — *empty vessels only*
*Boundary rule: contains/touches food → Dept 12 FnB; empty vessel → here. The multi-source / homogeneous-product home; scales with EAN matching when Uropa lands.*

- **Hot Food Packaging** `[size]` *(empty takeaway boxes, trays, clamshells)*
- **Cold Food Packaging** `[size]` *(empty portion pots, platter boxes & sleeves)*
- **Takeaway Cups & Lids** `[size]` → Paper/Takeaway Coffee Cups & Lids · Cold Drink Cups · Accessories
- **Disposable Tableware** `[]` → Disposable Glasses · Disposable Cutlery · Meal Trays · Disposable Serveware & Lids
- **Straws, Stirrers & Picks** `[]` → Straws · Drink Stirrers · Cocktail Sticks & Bamboo Picks *(empty; edible garnishes → Dept 12)*
- **Bags & Wraps** `[colour]` → Paper Bags · Clingfilm, Foil, Greaseproof & Parchment · Bin Bags & Refuse Sacks
- **Paper Hygiene & Wipes** `[]` → Blue Roll & Centrefeed · Toilet Roll & Hand Towels *(consumable; dispensers → Dept 9)*

### 9. Cleaning, Hygiene & Safety `HYG`
- **Chemicals & COSHH** `[]` *(regulated — `catalog.compliance.coshh_required`, evidence required)* → Dishwash/Glasswash Detergent & Rinse Aid · Sanitisers · Degreasers · Grill & Oven Cleaners · Washing-up Liquid · Ice Machine & Line Cleaner · Glass & Surface Cleaners · Glass Care & Renovation · Specialist Chemicals
- **Cleaning Equipment** `[]` → Mops, Buckets & Wringers · Brooms, Brushes & Dustpans · Squeegees & Deck Scrubs · Mop Heads & Handles · Vacuum Cleaners
- **Cloths, Wipes & Gloves** `[]` → Dish, J & Microfibre Cloths · Glass Cloths & Tea Towels · Sponges & Scourers · Surface & Probe Wipes · Washing-up & Disposable Gloves `[size]`
- **Washroom & Hygiene** `[]` — *level-2 target until volume* — hand soap & dispensers, paper hygiene dispensers *(durable)*, air fresheners, sanitary bins, toilet brushes & holders, hand dryers
- **Waste Management** `[]` → Kitchen, Pedal & Swing Bins · Washroom Bins
- **Health & Safety** `[]` — *level-2 target until volume* — first aid kits & refills, fire blankets, spillage kits, safety/wet-floor/mandatory signs, ashtrays & ashbins, anti-spiking

### 10. Clothing & Workwear (PPE) `CLO` — *reserved for Uropa scale*
- **Chef Wear** `[size, colour]` → Chef Jackets & Tunics · Chef Trousers · Chef Aprons
- **Aprons** `[size, colour]` → Bib · Waist
- **Headwear** `[size]` → Skull Caps, Beanies & Hats · Neckerchiefs
- **Front of House Uniform** `[size, colour]` → Waistcoats, Shirts & Uniforms
- **Footwear** `[size]` *(level-2 target until Uropa deepens it)*

### 11. Furniture, Fixtures & Display `FUR` — *reserved for Uropa scale; level 2 until then*
- **Furniture & Seating** `[colour]` — high chairs, seating, tables
- **Barriers & Queue Management** `[colour]` — barrier systems & ropes
- **Menus, Signs & Display** `[]` — menu holders, chalkboards & message boards, snap & poster frames, freestanding displays, leaflet dispensers, bar & licensing signs *(all non-safety POS display)*

### 12. Food & Beverage `FNB` — *reserved for Uropa's Food & Drink range; level 2 until then*
*Everything here is consumed or contains food. Each leaf carries extra `catalog.product_metadata` (allergen info, best-before) and may carry different carrier rules. No `capacity_lined` axis — a 70cl syrup is one variant; capacity is a spec, not a selector.*

- **Bar Syrups, Purées & Mixers** `[]` → Syrups · Purées & Cordials · Cocktail Mixers · Bitters *(liquid)*
- **Cocktail Garnishes & Rimmers** `[]` → Rimming Sugars & Salts · Cocktail Cherries, Onions & Olives · Dried-Fruit Garnishes
- **Soft Drinks & Water** `[]` — carbonated soft drinks, juices, bottled water, tonics & drink mixers
- **Tea, Coffee & Hot Beverages** `[]` — coffee & tea *as product* (beans, grounds, bags, leaf, pods), hot chocolate, matcha *(the vessels → Dept 2)*
- **Sauces, Condiments & Preserves** `[]` — table sauces & dips, dressings & oils, preserves, jams & honey
- **Ambient Groceries & Baking** `[]` — spices & seasonings, sugar & sweeteners, dessert toppings & dusters, bakery & baking ingredients
- **Snacks & Nuts** `[]` — nuts, crisps & snacks
- **Packaged Food to Go** `[]` — *pre-filled / food-containing* grab-and-go items *(the FnB side of the DIS boundary; empty packaging is Dept 8)*

---

## Single-home overlap resolution

One canonical `category_map` target each; the cross-cut is served by a collection or a filter, not a second node.

| Item | Canonical home | The other reference becomes |
|---|---|---|
| Disposable cutlery | **8** Disposable Tableware | — (removed from Cutlery) |
| Reusable plastic cutlery | **3** Table Cutlery | material = polycarbonate filter |
| Drink stirrers | **8** Straws, Stirrers & Picks | — (removed from Bar) |
| Cocktail sticks / bamboo picks *(empty)* | **8** Straws, Stirrers & Picks | edible garnishes → **12** |
| Garnish tweezers & tools *(reusable)* | **4** Ice, Garnish & Bar Tools | distinct from disposable picks |
| Sauce boats *(empty vessel)* | **2** Serveware & Presentation | the sauce inside → **12** |
| Steak knives & forks | **3** Serving & Speciality Cutlery | — (removed from Kitchen Knives) |
| Ice crushers *(manual)* | **4** Ice, Garnish & Bar Tools | electric ice machines → **7** |
| Chopping / cutting boards | **5** Prep Tools & Gadgets | bar knives stay in **4** |
| Paper hygiene *(consumable)* | **8** Paper Hygiene & Wipes | dispensers *(durable)* → **9** |
| Napkins | **2** Table Dressing & Service | — (cocktail napkins removed from Bar) |
| Hot-drink vessels *(latte glass, cup, mug)* | **2** Cups, Mugs & Saucers | — (removed from Dept 1) |
| Tiki & metal mugs | **4** Bar Sundries & Fixtures | — (removed from Depts 1 & 2) |
| Glass care & renovation | **9** Chemicals & COSHH | — (removed from Dept 1) |
| **Syrups, purées, cordials, cocktail mixers, bitters** | **12** Bar Syrups, Purées & Mixers | glass rimmers / empty bottles stay in **4** |
| **Rimming sugars & salts, dried-fruit garnishes, cocktail cherries/onions** | **12** Cocktail Garnishes & Rimmers | glass rimmer *tool* stays in **4** |
| **Food-containing to-go packs** | **12** Packaged Food to Go | empty boxes/pots/lids stay in **8** |
| Seasonings / sauces in a dispenser | **12** (the food) | the empty holder/bottle stays in **2** / **5** |
| **Coffee / tea as product** *(beans, grounds, bags, leaf, pods)* | **12** Tea, Coffee & Hot Beverages | anything you brew *into or with* — pot, cup, coffeeware → **2** |

**The DIS ↔ FnB test, stated once:** does food touch it? Yes → **12 FnB**. No (empty vessel) → its equipment/disposables department. This is what stops "Cocktail Cherries" and "Cocktail Sticks" — near-identical strings — from routing to the same node. The same test resolves the coffee/tea collision: the **thing you drink** is **12**; the **thing you brew it into or with** is **2**, so "Coffee Beans" and "Coffee Pot" never share a node.

---

## How this maps to the data model

- **`catalog.taxonomy`** — each node = one row (`category_id`, `parent_id`, `name`, `slug`, `path`, `variant_axes`), ready to load in option order.
- **`link.category_map`** — supplier `supplier_category_raw` → `category_id`. Level-2 and level-3 nodes are both valid targets. Unmapped strings → review queue. Uropa's "Food & Drink" raw strings now have real targets under Dept 12 instead of falling out of scope.
- **Level-1 freeze** — the 12 prefixes above are the immutable `MOS-<DEPT>` codes. `FNB` is reserved now precisely because it cannot be added after allocation begins.
- **Consumable metadata** — FnB leaves attract `allergen_information`, `best_before`, `dietary_preferences`, `ingredients` in `catalog.product_metadata`; these stay NULL for the rest of the catalogue (sparse key/value, as designed).
- **Compliance** — Chemicals & COSHH (Dept 9) and age-restricted lines carry `catalog.compliance` rows; FnB alcohol-adjacent or allergen claims route here too where evidence is required.
- **Multi-source** — homogeneous FnB lines (a given Monin syrup stocked by Uropa *and* Beaumont) link many supplier products to one `catalog.variant` via shared **EAN** first — FnB is likely where multi-source finally gets exercised at volume.

---

## Status

**v4 — freeze candidate.** Level 1 (12 departments + prefixes) is ready to freeze against SKU allocation, now including `FNB`. Open before freeze:

1. Confirm all four reserved prefixes (`EQP`, `CLO`, `FUR`, `FNB`) match how you'll shelve Uropa.
2. Decide whether `FNB` alcohol lines (if any) are in scope — they bring licensing/age-restriction compliance beyond ambient grocery.
3. Load `variant_axes` into `catalog.taxonomy`.

Levels 2–3 remain restructurable and will deepen (esp. Depts 7, 9, 11, 12) as Uropa's categories arrive.
