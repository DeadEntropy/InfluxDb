from influx_kwh import InfluxStateReader
from tqdm import tqdm
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytz
import math
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-date', default='2026-05-01', help='Start date YYYY-MM-DD (default: 2026-05-01)')
    args = parser.parse_args()

    start_date = datetime.strptime(args.start_date, '%Y-%m-%d').replace(tzinfo=pytz.UTC)
    end_date = datetime.today().replace(tzinfo=pytz.UTC)
    est = pytz.timezone('US/Eastern')

    entity_ids = ['thermostat_central', 'bedroom_thermostat', 'thermostat_extension']

    NB_DAY_BATCH = 10
    reader = InfluxStateReader()
    nb_batches = (end_date - start_date).days / NB_DAY_BATCH
    import_ranges = [
        (start_date + relativedelta(days=NB_DAY_BATCH * offset),
         start_date + relativedelta(days=NB_DAY_BATCH * (offset + 1)))
        for offset in range(math.ceil(nb_batches))
    ]

    all_frames = []
    for entity_id in entity_ids:
        stack = [reader.query_data(s, e, entity_id) for (s, e) in tqdm(import_ranges, desc=entity_id)]
        df = pd.concat([pd.DataFrame(s['state']) for s in stack if 'state' in s])
        df.index = df.index.tz_convert(est)
        all_frames.append(df)

    df_stack = pd.concat(all_frames)
    print(df_stack.head())
    return df_stack


if __name__ == '__main__':
    main()
