from setuptools import find_packages, setup

setup(
    name="blackbox",
    version="0.2.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    python_requires=">=3.11",
    install_requires=[
        "google-cloud-firestore>=2.29.0",
        "google-cloud-pubsub>=2.39.0",
        "google-adk>=2.1.0",
        "google-genai>=1.75.0",
        "opentelemetry-api>=1.20.0",
        "opentelemetry-sdk>=1.20.0",
        "opentelemetry-exporter-gcp-trace>=1.0.0",
        "fastapi>=0.115.0",
        "uvicorn>=0.30.0",
        "pydantic>=2.0.0",
        "python-ulid>=1.1.0",
        "python-dotenv>=1.0.0",
    ],
)
