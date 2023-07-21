# %%
import os
import logging
import warnings
import sys

import pandas as pd
import numpy as np
from tqdm import tqdm
from joblib import Parallel, delayed
import fire
from dotenv import load_dotenv

# Custom code
import pb_buddy.scraper as scraper
import pb_buddy.utils as ut
import pb_buddy.data_processors as dt
from pb_buddy.resources import get_category_list


def main(full_refresh=False, delay_s=1, num_jobs=4, categories_to_scrape=None):
    # TODO: Fix how we handle poor formatted inputs when using
    # workflow_dispatch vs. cron scheduled runs
    num_jobs = int(num_jobs) if num_jobs else 4
    full_refresh = False if not full_refresh else full_refresh

    category_dict = get_category_list()
    # Settings -----------------------------------------------------------------
    start_category = min(category_dict.values())
    end_category = max(category_dict.values())
    log_level = "INFO"
    show_progress = False if os.environ.get("PROGRESS", "0") == "0" else True

    # If categories to scrape not specified, scrape all
    if categories_to_scrape is None:
        categories_to_scrape = range(start_category, end_category + 1)
    else:
        categories_to_scrape = [int(x) for x in categories_to_scrape.split(",")]

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), None),
        format="%(asctime)s %(message)s",
    )

    logging.info("######## Starting new scrape session #########")
    all_base_data = dt.get_dataset(category_num=-1, data_type="base")
    all_sold_data = dt.get_dataset(category_num=-1, data_type="sold")
    logging.info("All previous data loaded from MongoDB")
    warnings.filterwarnings(action="ignore")

    # Main loop --------------------------------------------------
    # Iterate through all categories in random order, prevent noticeable patterns?

    num_categories_scraped = 0
    for category_to_scrape in np.random.choice(
        categories_to_scrape, size=len(categories_to_scrape), replace=False
    ):
        # Get category, some don't have entries so skip to next.(category 10 etc.)
        category_name = [x for x, v in category_dict.items() if v == category_to_scrape]
        if not category_name:
            continue

        logging.info(
            f"**************Starting Category {category_name} - Number: {category_to_scrape}. {num_categories_scraped} Categories Scraped So Far"
        )

        # Get existing open ads and stats of last scrape
        base_data = all_base_data.query("category_num == @category_to_scrape")

        # Setup date the compare ads to for refresh. If we want to check all current ads
        # for existence in our system, don't restrict based on date.
        if len(base_data) == 0:
            last_scrape_dt = pd.to_datetime("01-JAN-1900")
        else:
            if full_refresh:
                last_scrape_dt = pd.to_datetime("01-JAN-1900")
            else:
                last_scrape_dt = pd.to_datetime(base_data.datetime_scraped).max()

        # Setup for sold ad data
        sold_ad_data = all_sold_data.query("category_num == @category_to_scrape")

        # Get total number of pages of ads-----------------------------------------------
        total_page_count = scraper.get_total_pages(category_to_scrape)
        if total_page_count == 0:
            continue

        # Build List of Ad URLS----------------------------------------------------
        ad_urls = []
        logging.info(f"Scraping {total_page_count} pages of ads......")
        pages_to_check = list(range(1, total_page_count + 1))
        page_urls = [
            f"https://www.pinkbike.com/buysell/list/?region=3&page={x}&category={category_to_scrape}"
            for x in pages_to_check
        ]
        ad_urls = Parallel(n_jobs=num_jobs)(
            delayed(scraper.get_buysell_ads)(x, delay_s=delay_s)
            for x in tqdm(page_urls, disable=(not show_progress))
        )
        ad_urls = {key: value for d in ad_urls for key, value in d.items()}

        # Get new ad data ---------------------------------------------------------
        intermediate_ad_data = []
        intermediate_sold_ad_data = []
        logging.info(f"Extracting ad data from {len(ad_urls)} ads")

        # Do sequentially and check datetime of last scrape
        # Only add new ads. Note Pinkbike doesn't note AM/PM so can't do by time.
        # Have to round to date and check anything >= last scrape date.
        # Boosted ads are always at top of results, and may have older dates
        for url, boosted_status in tqdm(ad_urls.items(), disable=(not show_progress)):
            single_ad_data = scraper.parse_buysell_ad(url, delay_s=0)
            if single_ad_data != {}:
                if (
                    pd.to_datetime(single_ad_data["last_repost_date"]).date()
                    >= last_scrape_dt.date()
                    or boosted_status == "boosted"
                ):
                    # Sometimes sold ads kept in main results, ad for later
                    if (
                        "sold" in single_ad_data["still_for_sale"].lower()
                        and url not in sold_ad_data.url.values
                    ):
                        intermediate_sold_ad_data.append(single_ad_data)
                    else:
                        intermediate_ad_data.append(single_ad_data)
                else:
                    logging.info(
                        f"Checked up until ~ page: { (len(intermediate_ad_data)//20)+1} of ads"
                    )
                    break

        logging.info(
            f"Found: {len(intermediate_sold_ad_data)} sold ads in buysell normal pages"
        )
        # Split out brand new ads, versus updates ------------------
        if len(intermediate_ad_data) == 0:
            continue
        else:
            recently_added_ads = pd.DataFrame(intermediate_ad_data)
            logging.info(
                f"Found {len(recently_added_ads)} new ads, {len(intermediate_sold_ad_data)} sold ads"
            )
            # Check membership across categories in case of changes!
            new_ads = recently_added_ads.loc[
                ~recently_added_ads.url.isin(all_base_data.url), :
            ]
            logging.info(f"Found {len(new_ads)} new ads")

            # Get ads that might have an update. check price, description,title and category
            # for change. If category has changed, capture change here and update old entry.
            cols_to_check = ["price", "description", "ad_title", "category", "currency"]
            updated_ads = (
                recently_added_ads.loc[recently_added_ads.url.isin(all_base_data.url), :]
                .sort_values("url")
                .reset_index(drop=True)
            )
            previous_versions = (
                all_base_data.loc[all_base_data.url.isin(updated_ads.url)]
                .pipe(dt.get_latest_by_scrape_dt)
                .sort_values("url")
                .reset_index(drop=True)
            )
            unchanged_mask = (
                updated_ads[cols_to_check]
                .eq(previous_versions[cols_to_check])
                .all(axis=1)
            )

            # Filter to adds that actually had changes in columns of interest
            updated_ads = updated_ads.loc[~unchanged_mask.values, :]
            previous_versions = previous_versions.loc[~unchanged_mask.values, :]

            # Log the changes in each field
            if len(updated_ads) != 0:
                changes = ut.generate_changelog(
                    previous_ads=previous_versions,
                    updated_ads=updated_ads,
                    cols_to_check=cols_to_check,
                )
                if len(changes) > 0:
                    logging.info(f"{len(changes)} changes across {len(updated_ads)} ads")
                    dt.write_dataset(
                        changes.assign(category_num=category_to_scrape),
                        data_type="changes",
                    )

            # Update ads that had changes in key columns
            dt.update_base_data(
                updated_ads,
                index_col="url",
                cols_to_update=cols_to_check + ["datetime_scraped", "last_repost_date"],
            )

            # Write new ones !
            if len(new_ads) > 0:
                dt.write_dataset(
                    new_ads.assign(category_num=category_to_scrape), data_type="base"
                )

        logging.info(f"Adding {len(new_ads)} new ads to base data")

        # Get sold ad data ------------------------------------------------
        potentially_sold_urls = base_data.loc[
            ~base_data.url.isin(ad_urls.keys()), "url"
        ].dropna()
        # Rescrape sold ads to make sure, check for "SOLD"
        logging.info(
            f"Extracting ad data from {len(potentially_sold_urls)} potentially sold ads"
        )

        # Do sequentially and check datetime of last scrape
        # Only add new ads. Removed ads won't return a usable page.
        urls_to_remove = []
        for url in tqdm(potentially_sold_urls, disable=(not show_progress)):
            single_ad_data = scraper.parse_buysell_ad(url, delay_s=0)
            if (
                single_ad_data
                and "sold" in single_ad_data["still_for_sale"].lower()
                and url not in sold_ad_data.url.values
            ):
                intermediate_sold_ad_data.append(single_ad_data)
            else:
                urls_to_remove.append(url)

        logging.info(
            f"Found {len(intermediate_sold_ad_data)} sold ads, {len(urls_to_remove)} removed ads"
        )

        # If any sold ads found in normal listings, or missing from new scrape.
        # Write out to sold ad file. Deduplicate just in case
        if len(intermediate_sold_ad_data) > 0:
            new_sold_ad_data = (
                pd.DataFrame(intermediate_sold_ad_data)
                .dropna()
                .pipe(
                    ut.convert_to_float, colnames=["price", "watch_count", "view_count"]
                )
                .pipe(dt.get_latest_by_scrape_dt)
            )
            if len(new_sold_ad_data) > 0:
                dt.write_dataset(
                    new_sold_ad_data.assign(category_num=category_to_scrape),
                    data_type="sold",
                )

        # remove ads that didn't sell or sold and shouldn't be in anymore ---------------
        dt.remove_from_base_data(
            removal_df=base_data.loc[
                (base_data.url.isin(urls_to_remove))
                | (base_data.url.isin(sold_ad_data.url)),
                :,
            ],
            index_col="url",
        )
        logging.info(f"*************Finished Category {category_name}")

        num_categories_scraped += 1
        logging.info(
            f"Done Category {category_name} - Number: {category_to_scrape}. {num_categories_scraped} Categories Scraped So Far"
        )

    logging.info("######## Finished scrape session #########")


if __name__ == "__main__":
    load_dotenv("../.env")
    fire.Fire(main)
