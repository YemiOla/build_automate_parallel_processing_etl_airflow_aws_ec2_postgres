from airflow import DAG
from datetime import timedelta, datetime
from airflow.operators.dummy_operator import DummyOperator
from airflow.utils.task_group import TaskGroup
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.http.sensors.http import HttpSensor
import json
from airflow.providers.http.operators.http import SimpleHttpOperator
from airflow.operators.python import PythonOperator
import pandas as pd
from airflow.providers.postgres.hooks.postgres import PostgresHook


def kelvin_to_fahrenheit(temp_in_kelvin):
    temp_in_fahrenheit = (temp_in_kelvin - 273.15) * (9/5) + 32
    return round(temp_in_fahrenheit, 3)

def transform_load_data(task_instance):
    data = task_instance.xcom_pull(task_ids="group_a.tsk_extract_houston_weather_data")
    
    city = data["name"]
    weather_description = data["weather"][0]['description']
    temp_farenheit = kelvin_to_fahrenheit(data["main"]["temp"])
    feels_like_farenheit= kelvin_to_fahrenheit(data["main"]["feels_like"])
    min_temp_farenheit = kelvin_to_fahrenheit(data["main"]["temp_min"])
    max_temp_farenheit = kelvin_to_fahrenheit(data["main"]["temp_max"])
    pressure = data["main"]["pressure"]
    humidity = data["main"]["humidity"]
    wind_speed = data["wind"]["speed"]
    time_of_record = datetime.utcfromtimestamp(data['dt'] + data['timezone'])
    sunrise_time = datetime.utcfromtimestamp(data['sys']['sunrise'] + data['timezone'])
    sunset_time = datetime.utcfromtimestamp(data['sys']['sunset'] + data['timezone'])

    transformed_data = {"city": city,
                        "description": weather_description,
                        "temperature_farenheit": temp_farenheit,
                        "feels_like_farenheit": feels_like_farenheit,
                        "minimun_temp_farenheit":min_temp_farenheit,
                        "maximum_temp_farenheit": max_temp_farenheit,
                        "pressure": pressure,
                        "humidity": humidity,
                        "wind_speed": wind_speed,
                        "time_of_record": time_of_record,
                        "sunrise_local_time)":sunrise_time,
                        "sunset_local_time)": sunset_time                        
                        }
    transformed_data_list = [transformed_data]
    df_data = pd.DataFrame(transformed_data_list)
    
    df_data.to_csv("current_weather_data.csv", index=False, header=False)

def load_weather():
    hook = PostgresHook(postgres_conn_id= 'postgres_conn')
    hook.copy_expert(
        sql= "COPY weather_data FROM stdin WITH DELIMITER as ','",
        filename='current_weather_data.csv'
    )

def save_joined_data_s3(task_instance):
    data = task_instance.xcom_pull(task_ids="task_join_data")
    df = pd.DataFrame(data, columns = ['city', 'description', 'temperature_farenheit', 'feels_like_farenheit', 'minimun_temp_farenheit', 'maximum_temp_farenheit', 'pressure','humidity', 'wind_speed', 'time_of_record', 'sunrise_local_time', 'sunset_local_time', 'state', 'census_2020', 'land_area_sq_mile_2020'])
    # df.to_csv("joined_weather_data.csv", index=False)
    now = datetime.now()
    dt_string = now.strftime("%d%m%Y%H%M%S")
    dt_string = 'joined_weather_data_' + dt_string
    df.to_csv(f"s3://testing-ymlo/{dt_string}.csv", index=False)

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2023, 1, 8),
    'email': ['myemail@domain.com'],
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 2,
    'retry_delay': timedelta(minutes=2)
}


with DAG('weather_dag_2',
        default_args=default_args,
        schedule_interval = '@daily',
        catchup=False) as dag:

        start_pipeline = DummyOperator(
            task_id = 'tsk_start_pipeline'
        )

        join_data = PostgresOperator(
                task_id='task_join_data',
                postgres_conn_id = "postgres_conn",
                sql= '''SELECT 
                    w.city,                    
                    description,
                    temperature_farenheit,
                    feels_like_farenheit,
                    minimun_temp_farenheit,
                    maximum_temp_farenheit,
                    pressure,
                    humidity,
                    wind_speed,
                    time_of_record,
                    sunrise_local_time,
                    sunset_local_time,
                    state,
                    census_2020,
                    land_area_sq_mile_2020                    
                    FROM weather_data w
                    INNER JOIN city_look_up c
                        ON w.city = c.city                                      
                ;
                '''
            )

        load_joined_data = PythonOperator(
            task_id= 'task_load_joined_data',
            python_callable=save_joined_data_s3
            )

        end_pipeline = DummyOperator(
                task_id = 'task_end_pipeline'
        )

        with TaskGroup(group_id = 'group_a', tooltip= "Extract_from_S3_and_weatherapi") as group_A:
            create_table_1 = PostgresOperator(
                task_id='tsk_create_table_1',
                postgres_conn_id = "postgres_conn",
                sql= '''  
                    CREATE TABLE IF NOT EXISTS city_look_up (
                    city TEXT NOT NULL,
                    state TEXT NOT NULL,
                    census_2020 numeric NOT NULL,
                    land_Area_sq_mile_2020 numeric NOT NULL                    
                );
                '''
            )

            truncate_table = PostgresOperator(
                task_id='tsk_truncate_table',
                postgres_conn_id = "postgres_conn",
                sql= ''' TRUNCATE TABLE city_look_up;
                    '''
            )

            uploadS3_to_postgres  = PostgresOperator(
                task_id = "tsk_uploadS3_to_postgres",
                postgres_conn_id = "postgres_conn",
                sql = "SELECT aws_s3.table_import_from_s3('city_look_up', '', '(format csv, DELIMITER '','', HEADER true)', 'testing-ymlo', 'us_city.csv', 'us-west-2');"
            )

            create_table_2 = PostgresOperator(
                task_id='tsk_create_table_2',
                postgres_conn_id = "postgres_conn",
                sql= ''' 
                    CREATE TABLE IF NOT EXISTS weather_data (
                    city TEXT,
                    description TEXT,
                    temperature_farenheit NUMERIC,
                    feels_like_farenheit NUMERIC,
                    minimun_temp_farenheit NUMERIC,
                    maximum_temp_farenheit NUMERIC,
                    pressure NUMERIC,
                    humidity NUMERIC,
                    wind_speed NUMERIC,
                    time_of_record TIMESTAMP,
                    sunrise_local_time TIMESTAMP,
                    sunset_local_time TIMESTAMP                    
                );
                '''
            )

            is_houston_weather_api_ready = HttpSensor(
                task_id ='tsk_is_houston_weather_api_ready',
                http_conn_id='weathermap_api',
                endpoint='/data/2.5/weather?q=houston&APPID=3ef58669dda93f129d53eff4e6346905'
            )

            extract_houston_weather_data = SimpleHttpOperator(
                task_id = 'tsk_extract_houston_weather_data',
                http_conn_id = 'weathermap_api',
                endpoint='/data/2.5/weather?q=houston&APPID=3ef58669dda93f129d53eff4e6346905',
                method = 'GET',
                response_filter= lambda r: json.loads(r.text),
                log_response=True
            )

            transform_load_houston_weather_data = PythonOperator(
                task_id= 'transform_load_houston_weather_data',
                python_callable=transform_load_data
            )

            load_weather_data = PythonOperator(
            task_id= 'tsk_load_weather_data',
            python_callable=load_weather
            )



            create_table_1 >> truncate_table >> uploadS3_to_postgres
            create_table_2 >> is_houston_weather_api_ready >> extract_houston_weather_data >> transform_load_houston_weather_data >> load_weather_data
        start_pipeline >> group_A >> join_data >> load_joined_data >> end_pipeline