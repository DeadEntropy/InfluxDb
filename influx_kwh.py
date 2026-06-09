import os
from dotenv import load_dotenv
from influxdb import InfluxDBClient, DataFrameClient
from datetime import datetime
import pandas as pd


def get_influx_client(df=False):
    load_dotenv()
    cls = DataFrameClient if df else InfluxDBClient
    client = cls(
        host=os.environ['INFLUX_HOST'],
        port=int(os.environ.get('INFLUX_PORT', 8086)),
        username=os.environ['INFLUX_USERNAME'],
        password=os.environ['INFLUX_PASSWORD'],
    )
    client.switch_database(os.environ['INFLUX_DBNAME'])
    return client


class InfluxTempReader:
    @staticmethod
    def _get_query(s, e):
        return f'SELECT "value" FROM "°F" WHERE time >= \'{s}\' AND time < \'{e}\' GROUP BY "domain","entity_id" '

    def __init__(self):
        self._client = get_influx_client()

    @staticmethod
    def to_temperature_df(entity_id, data):
        df = pd.DataFrame(columns=['entity_id', 'time', 'Temperature'])
        df['time'] = [x[0] for x in data]
        df['Temperature'] = [x[1] for x in data]
        df['entity_id'] = entity_id
        return df

    def query_data(self, start_date: datetime, end_date: datetime, resample='5 min'):
        s = start_date.strftime("%Y-%m-%d")
        e = end_date.strftime("%Y-%m-%d")
        results = self._client.query(InfluxTempReader._get_query(s, e))
        if len(results.raw['series']) == 0:
            return pd.DataFrame(columns=['entity_id', 'time', 'Temperature'])
        tags = [x['tags']['entity_id'] for x in results.raw['series']]
        df = pd.concat([InfluxTempReader.to_temperature_df(x['tags']['entity_id'], x['values']) for x in results.raw['series'] if x['tags']['entity_id'] in tags])
        df.time = pd.to_datetime(df.time, format='ISO8601')

        df_stack = pd.DataFrame(pd.pivot_table(df.reset_index(), index='time', columns='entity_id', values='Temperature', aggfunc='sum').to_records())
        df_stack = df_stack.ffill().bfill().set_index('time')
        df_stack = df_stack.loc[start_date:]

        return df_stack.resample(resample).mean()

    def format_entity_id(s):
        return "_".join(s.split('_')[:-3])


class InfluxKwhReader:
    @staticmethod
    def _get_query(s, e):
        return f'SELECT "value" FROM "kWh" WHERE time >= \'{s}\' AND time < \'{e}\' GROUP BY "domain","entity_id" '

    _master_kwh = 'emporia_vue_123_1d'

    def __init__(self):
        self._client = get_influx_client()

    @staticmethod
    def to_kwh_df(entity_id, data):
        df = pd.DataFrame(columns=['entity_id', 'time', 'kWh'])
        df['time'] = [x[0] for x in data]
        df['kWh'] = [x[1] for x in data]
        df['entity_id'] = entity_id
        return df

    def query_data(self, start_date: datetime, end_date: datetime, resample='5 min'):
        s = start_date.strftime("%Y-%m-%d")
        e = end_date.strftime("%Y-%m-%d")
        results = self._client.query(InfluxKwhReader._get_query(s, e))
        tags = [x['tags']['entity_id'] for x in results.raw['series'] if x['tags']['entity_id'].endswith('_1d')]
        df = pd.concat([InfluxKwhReader.to_kwh_df(x['tags']['entity_id'], x['values']) for x in results.raw['series'] if x['tags']['entity_id'] in tags])
        df.time = pd.to_datetime(df.time, format='ISO8601')

        df_stack = pd.DataFrame(pd.pivot_table(df.reset_index(), index='time', columns='entity_id', values='kWh', aggfunc='sum').to_records())
        df_stack = df_stack.ffill().bfill().set_index('time')
        df_stack = df_stack.loc[start_date:]
        df_stack['Residual'] = df_stack[self._master_kwh] - (df_stack.sum(axis=1) - df_stack[self._master_kwh])

        return df_stack.resample(resample).mean()


class InfluxStateReader:
    @staticmethod
    def _get_query(s, e, entity):
        return (
            f"SELECT * FROM \"homeassistant\".\"autogen\".\"state\" "
            f"WHERE time >= '{s}' AND time < '{e}' AND (\"entity_id\"='{entity}')"
        )

    def __init__(self):
        self._client = get_influx_client(df=True)

    def query_data(self, start_date: datetime, end_date: datetime, entity: str):
        s = start_date.strftime("%Y-%m-%d")
        e = end_date.strftime("%Y-%m-%d")
        return self._client.query(InfluxStateReader._get_query(s, e, entity))


class KwhToPrice:
    _kwh_price = 0.12302
    _additional_kwh_price = 0.14294
    _base_charge = 9.48
    _taxes = 36.58

    def price(self, kwh_consumed, days: int = 30):
        price = self._base_charge + self._taxes
        price += self.incremental_price(kwh_consumed, days)
        return price

    def incremental_price(self, kwh_consumed, days: int = 30):
        price = min(1000, kwh_consumed * days) * self._kwh_price
        price += max(0, kwh_consumed * days - 1000) * self._additional_kwh_price
        return price


def format_entity_id(entity_id):
    entity_split = entity_id.split('_')
    if len(entity_split) == 4 and entity_split[2] == 'balance':
        return 'Balance'
    return " ".join([w.capitalize() for w in entity_split[:-2]])
