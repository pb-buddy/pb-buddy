import os
import itertools
from typing import List

from fastapi import FastAPI, Body
from autogluon.multimodal import MultiModalPredictor
from joblib import load
import pandas as pd
from model import BikeBuddyAd, BikeBuddyAdPredictions
import predictor as predictor
from dotenv import load_dotenv

load_dotenv(".env")

app = FastAPI(
    title="Bike Buddy API", description="API bike price prediction", version="0.0"
)


@app.on_event("startup")
async def load_model():
    predictor.pipeline = load(os.environ["PIPELINE_FILE"])
    predictor.model = MultiModalPredictor.load(os.environ["MODEL_FILE"])


@app.post("/text-predict", tags=["text-predictions"])
async def predict(ads: List[BikeBuddyAd]) -> BikeBuddyAdPredictions:
    # Due to sklearn pipeline needing our target column price_cpi_adjusted_CAD
    # need to ensure it's present for transform() method
    df_input = pd.DataFrame(data=[dict(single_ad) for single_ad in ads]).assign(
        price_cpi_adjusted_CAD=0
    )

    # # TODO: confirm column names needed for pipeline:
    # required_input_cols = set(
    #     itertools.chain(
    #         *[
    #             step[2]
    #             for step in predictor.pipeline.named_steps["preprocess"].transformers_[
    #                 :-1
    #             ]
    #         ]
    #     )
    # )

    df_predict = pd.DataFrame(
        data=predictor.pipeline.transform(df_input),
        columns=predictor.pipeline.get_feature_names_out(),
    )

    predictions = predictor.model.predict(df_predict, as_pandas=False).tolist()
    if type(predictions) == float:
        predictions = [predictions]
    outputs = BikeBuddyAdPredictions(predictions=predictions)

    return outputs