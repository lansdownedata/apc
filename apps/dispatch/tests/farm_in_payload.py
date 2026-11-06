"""GNet's sample farm-in reservation (contract: GNET-CONNECTION-GUIDE.md §1)."""

import copy

_PAYLOAD = {
    "transactionId": "935a1056-644c-4e82-9a1a-6099efdd056b",
    "reservationDate": "-07:00",
    "reservationType": "REGULAR",
    "submitMode": "WAIT",
    "runType": "Airport",
    "preferredVehicleType": "SEDAN",
    "passengerCount": "2",
    "notes": [
        {"context": "AFFILIATE", "text": "Please confirm receipt"},
        {"context": "DRIVER", "text": "Meet at curb"},
    ],
    "namesignInstructions": "Mr. Adam",
    "specialInstructions": "- Carseat needed ",
    "affiliateReservation": {
        "action": "NEW",
        "status": "BOOKING_REQUEST",
        "requesterId": "gnettest",
        "requesterResNo": "1715016686",
        "providerId": "LATtest",
    },
    "passengers": [
        {
            "firstName": "Alex",
            "lastName": "Smith",
            "email": "alex.smith@example.com",
            "phoneNumber": "+155550044444",
        }
    ],
    "locations": {
        "pickup": {
            "address": "123 S Beverly Dr, Beverly Hills, CA 90212, USA",
            "lat": 34.0664703,
            "lon": -118.3993243,
            "time": "2024-08-03T10:27:22",
        },
        "stops": [
            {
                "address": "9876 Wilshire Blvd, Beverly Hills, CA 90210, USA",
                "lat": 34.0669,
                "lon": -118.4005,
            }
        ],
        "dropOff": {
            "address": "LAX",
            "locationType": "airport",
            "meetAndGreet": "False",
            "flightInfo": {"flightNumber": "123", "airlineCode": "UA"},
        },
    },
}


def sample_payload() -> dict:
    """A fresh deep copy so tests can mutate freely."""
    return copy.deepcopy(_PAYLOAD)
