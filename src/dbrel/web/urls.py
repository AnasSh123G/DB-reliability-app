from __future__ import annotations

from django.urls import path

from dbrel.web import views

urlpatterns = [
    path("", views.index, name="index"),
    path("station/<int:station_id>/", views.station, name="station"),
    path("train/<int:train_id>/", views.train, name="train"),
    path("line/<int:line_id>/", views.line, name="line"),
    path("api/suggest", views.suggest, name="suggest"),
    path("api/station/<int:station_id>/board", views.station_board, name="station-board"),
    path("api/train/<int:train_id>/route", views.train_route, name="train-route"),
    path("api/line/<int:line_id>/directions", views.line_directions, name="line-directions"),
    path("api/line/<int:line_id>/stations", views.line_stations, name="line-stations"),
    path("api/line/<int:line_id>/around", views.line_trips_around, name="line-trips-around"),
]
