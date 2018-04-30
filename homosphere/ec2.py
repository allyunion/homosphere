
# Import python standard libraries
import ipaddress
from math import ceil, log

# Import custom libraries
import boto3

class VPC:
    def __init__(self, name, network, region, aws_access_key = None, aws_secret_key = None, aws_profile = 'default'):
        self.name = name
        self.ipv4network = ipaddress.IPv4Network(network)
        self.region = region
        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.session = None
        self.availability_zones = []
        self.subnet_prefixlen = 0
        if aws_profile is not None:
            self.session = boto3.Session(profile_name = aws_profile,
                                         region_name = region)
            creds = self.session.get_credentials()
            self.aws_access_key = creds.access_key
            self.aws_secret_key = creds.secret_key
        elif not None in (aws_access_key, aws_secret_key):
            self.session = boto3.Session(aws_access_key_id = aws_access_key,
                                         aws_secret_access_key = aws_secret_key,
                                         region_name = region)

    def nearest_power_of_2(self, number):
        return int(pow(2, ceil(log(number, 2))))

    def get_availability_zones(self):
        client = self.session.client('ec2')
        self.availability_zones = [item['ZoneName'] 
                for item in client.describe_availability_zones()[
                    'AvailabilityZones']]

    def calculate_subnet_prefixlen(self):
        if len(self.availability_zones) == 0:
            self.get_availability_zones()
        num_of_zones = len(self.availability_zones) * 2
        nearest = self.nearest_power_of_2(num_of_zones)
        subnets = [next(self.ipv4network.subnets(new_prefix=item))
                for item in range(self.ipv4network.prefixlen + 1, 33)]
        subnet_hosts = [item.num_addresses for item in subnets]
        required_addresses = self.ipv4network.num_addresses / nearest
        subnet = subnets[subnet_hosts.index(required_addresses)]
        self.subnet_prefixlen = subnet.prefixlen
