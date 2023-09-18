from setuptools import setup, find_packages

with open('requirements.txt') as fr:
    requirements = fr.readlines()

setup(
    name='w3ext',
    version='0.0.1',
    author_email='chewba34@gmail.com',
    packages=find_packages('src'),
    package_dir={'': 'src'},
    package_data={"": ["abi/*.json"]},
    include_package_data=True,
    install_requires=requirements,
)