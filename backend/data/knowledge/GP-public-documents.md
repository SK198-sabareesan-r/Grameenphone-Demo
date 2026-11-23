# Grameenphone official public documents (knowledge base sources)

All links below are on grameenphone.com or its official subdomains. They are the source list for the knowledge pack. Nothing here is mock data.

## How the language split was decided

- Pages come from GP's own sitemap. Bangla pages use the `/bn/` prefix.
- Each key page was fetched in English and in Bangla, with a 10-second delay between requests as the site's `robots.txt` asks. The check counts Bengali characters in the visible text.
- **Bangla (full):** more than 500 Bengali characters, so the body is in Bangla.
- **Bangla (partial):** the `/bn/` page loads but has few Bengali characters. The body is probably still English, with Bangla only in the menu. Review these by hand before using them as Bangla knowledge.
- **No Bangla page:** the `/bn/` address is listed in the sitemap or exists, but returns "not found".
- Page content changes. Record the fetch date on every article that is built from these pages, and check any price, fee or code that will appear on screen.

## Conflicts to resolve before the demo

| Item | What the pages say | Pages |
|---|---|---|
| Charge after an internet pack's data or validity ends | One page gives a cap of Tk 5.825 per day. Another gives a cap of Tk 6.95. | [General information about internet packs](https://www.grameenphone.com/general-information-about-internet-packs), [30 GB Tk 599 offer](https://www.grameenphone.com/bn/personal/plans-offers/offers/30-gb-tk-599) |
| Carry-forward of unused data | The public rule says leftover data is added when the same pack is bought again within the validity period. The playbook's CP8 example says "within 3 days (sample rule)". | Same two pages, and the playbook |

Pick one rule for CP8 and say clearly on screen whether it is GP's public rule or a sample.

---

## A. English documents

### A1. Packs, internet and offers

| Topic | Document | URL | Used for | In knowledge base |
|---|---|---|---|---|
| Internet pack rules (auto-renew, balance check, cancel, charges, carry-forward) | General information about internet packs | https://www.grameenphone.com/general-information-about-internet-packs | CP1, CP3, CP8 | Yes: `internet-packs-general` |
| Internet pack list | Internet packages | https://www.grameenphone.com/personal/plans-offers/internet-packages | CP3 | Partly |
| Data and minutes bundles | Bundle packs | https://www.grameenphone.com/personal/plans-offers/internet-packages/bundle-packs | CP3 | Yes: `bundle-packs` (snapshot) |
| Single pack example | 5 GB internet pack | https://www.grameenphone.com/personal/plans-offers/offers/5gb-internet-pack | CP3 | To collect |
| Campaign questions | Campaign FAQ | https://www.grameenphone.com/campaign-faq | CP3 | Fetched, not yet an article |
| Prepaid and postpaid plans | Packages overview | https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages | CP3 | To collect |
| Postpaid plan | Prime | https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages/prime | CP2 | To collect |
| Prime questions | Prime help and FAQ | https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages/prime/help-and-faq | CP2 | To collect |
| Prime price and credit limit | Prime price plan | https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages/prime/prime-price-plan-credit-limit | CP2 | To collect |
| Flat-rate voice plan | Nishchinto | https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages/nishchinto | Extra | To collect |
| Voice plan | 1 Number Plan | https://www.grameenphone.com/personal/plans-offers/prepaid-postpaid-packages/1-number-plan | Extra | To collect |
| Tourist SIM | Tourist SIM | https://www.grameenphone.com/personal/plans-offers/offers/tourist-sim | Extra | To collect |

### A2. Roaming (Malaysia trip, CP10 and CP6)

| Topic | Document | URL | Used for | In knowledge base |
|---|---|---|---|---|
| Roaming overview | Roaming | https://www.grameenphone.com/personal/services/roaming | CP6, CP10 | Partly: `roaming-basics` |
| Activation | Roaming activation | https://www.grameenphone.com/personal/services/roaming/activation | CP6, CP10 | Yes: `roaming-activation` |
| Rates | Roaming rates | https://www.grameenphone.com/personal/services/roaming/rate | CP10 | To collect |
| Bill payment | Roaming bill payment | https://www.grameenphone.com/personal/services/roaming/billpay | CP10 | To collect |
| Bill-shock protection | TravelSure | https://www.grameenphone.com/personal/services/roaming/travelsure | CP10 | Yes: `roaming-travelsure` |
| Questions | Roaming FAQ | https://www.grameenphone.com/personal/services/roaming/faq | CP6, CP10 | Not collected: refused automated fetching (403). Read it in a browser. |
| Prepaid roaming and documents | Prepaid roaming | https://www.grameenphone.com/personal/services/roaming/prepaid-roaming | CP6, CP10 | Partly, from a search excerpt (`roaming-activation`). The page refused automated fetching (403), so read it in a browser. |
| Bonus roaming packs | Bonus roaming packs | https://www.grameenphone.com/personal/plans-offers/offers/bonus-roaming-packs | CP10 | To collect |
| Roaming site, pack pages | Roaming packs (for example 20 GB) | https://roaming.grameenphone.com/personal/services/roaming/popular-offers/20-gb-internet | CP6, CP10 | Partly: `roaming-basics` |
| Roaming site, internet packages | Roaming internet packages | https://roaming.grameenphone.com/personal/plans-offers/internet-packages | CP6 | To collect |
| Roaming site, country pages | Per-country rates (Malaysia page needed) | https://roaming.grameenphone.com (find the Malaysia page from the home page) | CP10 | To collect |
| Roaming site help | Useful information | https://roaming.grameenphone.com/useful-information | CP6 | To collect |

### A3. SIM, ownership and eSIM (CP9)

| Topic | Document | URL | Used for | In knowledge base |
|---|---|---|---|---|
| SIM replacement | SIM replacement | https://www.grameenphone.com/shop/sim-replacement | CP9 | Yes: `sim-replacement` |
| SIM services | SIM services | https://www.grameenphone.com/shop/sim-services | CP9 | To collect |
| Ownership transfer | SIM ownership transfer | https://www.grameenphone.com/shop/sim-ownership-transfer | CP9 | To collect |
| Number portability | Mobile number portability | https://www.grameenphone.com/shop/mobile-number-portability | Extra | To collect |
| eSIM | eSIM | https://www.grameenphone.com/esim | Extra | To collect |
| Nearest centre | Store locator | https://www.grameenphone.com/personal/customer-service/store-locator | CP9 | To collect |

### A4. Bill, recharge and value-added services (CP2)

| Topic | Document | URL | Used for | In knowledge base |
|---|---|---|---|---|
| Pay a postpaid bill | Postpaid bill payment | https://www.grameenphone.com/personal/services/vas-others/postpaid-bill-payment | CP2 | Yes: `postpaid-bill-payment` |
| E-bill | eBill | https://www.grameenphone.com/personal/services/vas-others/ebill | CP2 | Yes (same article) |
| Balance transfer | Balance transfer | https://www.grameenphone.com/personal/services/vas-others/balance-transfer | CP2 | To collect |
| Recharge | Private recharge | https://www.grameenphone.com/personal/services/vas-others/private-recharge | CP2 | To collect |
| Recharge terms | Terms for GP recharge | https://www.grameenphone.com/bn/terms-and-conditions-for-gp-recharge | CP2 | Yes: `recharge-terms` (English text from `https://www.grameenphone.com/terms-and-conditions-for-gp-recharge`) |
| Value-added services overview | VAS and others | https://www.grameenphone.com/personal/services/vas-others | CP2 | To collect |
| VAS example, how to unsubscribe | News service | https://www.grameenphone.com/personal/services/vas/news-service | CP2 | Yes: `vas-unsubscribe` |
| VAS example | Missed call alert | https://www.grameenphone.com/personal/services/vas/missed-call-alert | CP2 | Yes: `vas-unsubscribe` |
| VAS example | Call block | https://www.grameenphone.com/personal/services/vas/call-block | CP2 | Yes: `vas-unsubscribe` |
| VAS example | Islamic service | https://www.grameenphone.com/personal/services/vas/islamic-service | CP2 | To collect |
| VAS example | Welcome tune | https://www.grameenphone.com/personal/services/vas/welcome-tune | CP2 | To collect |
| International tariff | ISD tariff | https://www.grameenphone.com/personal/services/vas/isd-tariff | Extra | To collect |
| Payments app | GPay | https://www.grameenphone.com/personal/services/financial-services/gpay | Extra | To collect |

### A5. Help, troubleshooting and policies (CP1, CP6)

| Topic | Document | URL | Used for | In knowledge base |
|---|---|---|---|---|
| Phone and network settings, MyGP how-tos | Useful information | https://www.grameenphone.com/useful-information | CP1 | Yes: `internet-troubleshooting` |
| Handset settings | Handset settings | https://www.grameenphone.com/personal/services/vas-others/handset-settings | CP1 | To collect |
| Self-service app | MyGP app | https://www.grameenphone.com/personal/services/digital-services/mygp-app | CP6 | To collect |
| Self-service app | App mobile self-service | https://www.grameenphone.com/personal/digital-services/other-services/gp-app-mobile-self-service | CP6 | To collect |
| Customer service channels | Online customer service | https://www.grameenphone.com/personal/customer-service/online-customer-service | CP6 | Yes: `customer-contacts` |
| Contact numbers | Useful contacts | https://www.grameenphone.com/personal/customer-service/useful-contacts | CP6 | Yes: `customer-contacts` |
| Customer service home | Customer service | https://www.grameenphone.com/personal/customer-service | CP6 | To collect |
| Useful notices | Notice board | https://www.grameenphone.com/useful-links/notice-board | Extra | To collect |
| Deactivation of unused connections | Notice example | https://www.grameenphone.com/notices-board-345 | Extra | To collect |
| Terms of use | Terms of use | https://www.grameenphone.com/terms-use | Extra | To collect |
| Privacy | Privacy policy | https://www.grameenphone.com/privacy-policy | Extra | To collect |
| Privacy | Privacy notice | https://www.grameenphone.com/privacy-notice | Extra | To collect |

### A6. PDF and file documents (English)

These are the only true files in the list. They are the only sources where BDA could add value. Everything else in this document is a web page.

| Topic | Document | URL |
|---|---|---|
| gpfi general terms of service | gpfi terms, one release | https://gpfi-bo.grameenphone.com/sites/default/files/2026-08/gpfi_TnC_19_August_2026.pdf |
| gpfi general terms of service | gpfi terms, earlier release | https://gpfi-bo.grameenphone.com/sites/default/files/2026-03/gpfi_TnC_March_2026.pdf |
| gpfi terms (bKash), English page | gpfi (bKash) terms | https://www.grameenphone.com/gpfibkashterms-conditions-url-english |
| International roaming subscription form | Linked as "Download Form" on the prepaid roaming page | https://www.grameenphone.com/personal/services/roaming/prepaid-roaming |

---

## B. Bangla documents

### B1. Bangla pages with full Bangla text

| Topic | Document | URL | Bengali characters found | English counterpart |
|---|---|---|---|---|
| Internet packs | ইন্টারনেট প্যাক | https://www.grameenphone.com/bn/personal/plans-offers/internet-packages | about 1,100 | A1 |
| Internet pack, entertainment | Entertainment packs | https://www.grameenphone.com/bn/personal/plans-offers/internet-packages/entertainment | not counted | none in the list |
| Offer page, 30 GB Tk 599 | Offer page | https://www.grameenphone.com/bn/personal/plans-offers/offers/30-gb-tk-599 | not counted (body text partly English) | none in the list |
| Plans overview | Packages overview | https://www.grameenphone.com/bn/personal/plans-offers/prepaid-postpaid-packages | about 940 | A1 |
| Prime help and FAQ | Prime FAQ | https://www.grameenphone.com/bn/personal/plans-offers/prepaid-postpaid-packages/prime/help-and-faq | about 2,900 | A1 |
| Postpaid bill payment | Bill payment | https://www.grameenphone.com/bn/personal/services/vas-others/postpaid-bill-payment | about 6,500 | A4 |
| E-bill | eBill | https://www.grameenphone.com/bn/personal/services/vas-others/ebill | about 1,900 | A4 |
| Balance transfer | Balance transfer | https://www.grameenphone.com/bn/personal/services/vas-others/balance-transfer | about 1,200 | A4 |
| VAS and others | VAS overview | https://www.grameenphone.com/bn/personal/services/vas-others | about 1,400 | A4 |
| MyGP app | MyGP app | https://www.grameenphone.com/bn/personal/services/digital-services/mygp-app | about 1,600 | A5 |
| App self-service | App self-service | https://www.grameenphone.com/bn/personal/digital-services/other-services/gp-app-mobile-self-service | about 980 | A5 |
| Online customer service | Customer service channels | https://www.grameenphone.com/bn/personal/customer-service/online-customer-service | about 640 | A5 |
| Useful contacts | Contact numbers | https://www.grameenphone.com/bn/personal/customer-service/useful-contacts | about 890 | A5 |
| Store locator | GP Centres | https://www.grameenphone.com/bn/personal/customer-service/store-locator | about 37,700 (mostly store names and addresses) | A3 |
| Terms of use | Terms | https://www.grameenphone.com/bn/terms-use | about 15,700 | A5 |
| Privacy policy | Privacy | https://www.grameenphone.com/bn/privacy-policy | about 13,000 | A5 |
| gpfi (bKash) terms, Bangla page | gpfi terms | https://www.grameenphone.com/gpfibkashterms-conditions-url-bangla | not counted | A6 |

### B2. Bangla pages that load but look mostly English (review by hand)

| Topic | URL | Bengali characters found | Why it matters |
|---|---|---|---|
| General information about internet packs | https://www.grameenphone.com/bn/general-information-about-internet-packs | about 120 | This is the main source for pack rules and the carry-forward answer |
| SIM replacement | https://www.grameenphone.com/bn/shop/sim-replacement | about 70 | Needed for CP9 |
| Useful information (troubleshooting) | https://www.grameenphone.com/bn/useful-information | about 170 | Needed for CP1 |
| Handset settings | https://www.grameenphone.com/bn/personal/services/vas-others/handset-settings | about 360 | CP1 |
| ISD tariff | https://www.grameenphone.com/bn/personal/services/vas/isd-tariff | about 170 | Extra |
| Prime | https://www.grameenphone.com/bn/personal/plans-offers/prepaid-postpaid-packages/prime | about 360 | CP2 |
| Tourist SIM | https://www.grameenphone.com/bn/personal/plans-offers/offers/tourist-sim | about 110 | Extra |
| Notice board | https://www.grameenphone.com/bn/useful-links/notice-board | about 160 | Extra |
| GP recharge terms | https://www.grameenphone.com/bn/terms-and-conditions-for-gp-recharge | not counted, snippet is English | CP2 |

For these pages the Bangla version either doesn't exist or is only the page frame. If the demo needs a Bangla answer on these topics (pack rules, SIM replacement, troubleshooting), we have to write the Bangla articles ourselves from the English source and have a Bangla speaker review them.

### B3. Topics with no Bangla page found

| Topic | Result |
|---|---|
| Roaming overview, activation, rates | The `/bn/` address is in the sitemap but returns "not found" |
| Roaming site (`roaming.grameenphone.com`) | English only, as far as the search showed |
| Prepaid roaming, TravelSure, roaming FAQ | No Bangla page found |
| Single-user 15-SIM limit page | Not found in either language at the address in the sitemap |
| SIM ownership transfer, eSIM, number portability, value-added service example pages | Not checked for Bangla |

Roaming is the biggest gap for the Bangla side. CP6 and CP10 both depend on it, so the Bangla roaming article has to be written by us.

---

## C. Summary of the gaps for the demo

| Needed for | English | Bangla |
|---|---|---|
| CP6 roaming answer | Source pages exist, Malaysia page still to collect | No official Bangla page. Write and review our own. |
| CP9 lost SIM | Written (`sim-replacement`) | Official page is mostly English. Write and review our own. |
| CP2 bill, value-added service | Bill payment written. VAS examples still to collect. | Official Bangla pages exist for bill and VAS overview |
| CP1 internet troubleshooting | Written (`internet-troubleshooting`) | Official page is mostly English. Write and review our own. |
| CP3 bigger pack | Pack lists exist, check prices | Official Bangla pack list exists |
| CP8 carry-forward | Public rule exists. Choose between it and the sample. | Official Bangla page is mostly English |

## D. Collection rules

- `roaming.grameenphone.com` disallows all automated access in its `robots.txt` (`Disallow: /`). Do not fetch it with scripts. Read those pages by hand in a browser. The page-to-PDF script skips every page on that host, and also any page that redirects there.

- Fetch only a few dozen pages. Wait 10 seconds between requests (`Crawl-delay` in the site's `robots.txt`).
- Do not fetch paths that `robots.txt` blocks, such as `/search/`, `/dashboard`, `/personal/plans-offers/internet-packages/1.5-gb` and `/personal/plans-offers/offers/recharge-validity-matrix`.
- Save each page as one markdown article with the source URL, the fetch date and `type: public` in its front matter. Keep codes, prices and fees exactly as published.
- Write the Bangla articles from the English source where no official Bangla page exists, and mark them as written by us.

---

## E. Collected in the latest round

| Topic | Source | Article | Note |
|---|---|---|---|
| Malaysia roaming pack (10 GB, 10 days, BDT 1599) and other roaming packs | https://roaming.grameenphone.com/personal/services/roaming/popular-offers/10-gb-internet | `roaming-malaysia-pack` | The page lists Thailand, Malaysia, Singapore and India. Prices and countries can change, so recheck before the demo. |
| Roaming activation | https://www.grameenphone.com/personal/services/roaming/activation | `roaming-activation` | Prepaid details come from a search excerpt of the prepaid roaming page |
| TravelSure codes | https://www.grameenphone.com/personal/services/roaming/travelsure | `roaming-travelsure` | |
| VAS unsubscribe steps (four examples) | missed-call-alert, news-service, study-line, call-block pages under https://www.grameenphone.com/personal/services/vas/ | `vas-unsubscribe` | The refund policy itself is not public and stays mock |
| Recharge terms and recharge not showing | https://www.grameenphone.com/terms-and-conditions-for-gp-recharge | `recharge-terms` | The steps for an agent are our wording, built from the rules on the page |
| Customer service contacts | https://www.grameenphone.com/personal/customer-service/useful-contacts | `customer-contacts` | |

## F. Still not available from public GP pages

- **Lost SIM block procedure.** No public page describes it. The SIM replacement article now says so and points to the public contact channels.
- **Roaming FAQ and roaming tariff pages.** Both refused automated requests (403) on `www.grameenphone.com` and on `roaming.grameenphone.com`. They need to be read in a browser and copied by hand. Do not work around the block.
- **Prepaid roaming page in full.** Same block. Only a search excerpt was used.
- **Internal policies:** value-added service refunds, network outage goodwill, compliance rules, network notices. These stay mock and are labelled so.
