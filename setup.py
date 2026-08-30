from setuptools import setup, find_packages

setup(
    name="blackbox",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "google-cloud-firestore>=2.13.0",
        "google-cloud-trace>=1.0.0",
        "opentelemetry-api>=1.20.0",
        "opentelemetry-sdk>=1.20.0",
        "opentelemetry-exporter-gcp-trace>=1.0.0",
        "pydantic>=2.0.0",
        "python-ulid>=1.1.0",
    ],
)
