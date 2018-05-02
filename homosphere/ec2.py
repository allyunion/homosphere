"""Module helper around VPCs in EC2 for CloudFormation"""

# pylint ignores
# pylint: disable=too-many-arguments

# Import python standard libraries
import ipaddress
from math import ceil, log
import re

# Import custom libraries
import boto3

# Import troposphere
import troposphere
from troposphere import Export, GetAtt, Output, Ref, Template, Sub
from troposphere.ec2 import EIP, InternetGateway, NatGateway
from troposphere.ec2 import Route, RouteTable
from troposphere.ec2 import Subnet, SubnetRouteTableAssociation
from troposphere.ec2 import Tag, VPCGatewayAttachment

TITLE_CLEANUP_RE = re.compile(r'[^a-zA-Z0-9]+')

def nearest_power_of_2(number):
    """Returns the nearest power of 2 that is greater than a given number"""
    return int(pow(2, ceil(log(number, 2))))

class VPC:
    """Helper around definiting a VPC in AWS, and define subnets based on
    an IP network given, divided equally up with assumption of public + private
    subnet for each availability zone given a AWS region"""
    def __init__(self, name, network, region, tags=None, aws_access_key=None,
                 aws_secret_key=None, aws_profile='default'):
        """Constructor"""
        self.data = {}
        self.data['Name'] = name
        self.data['Title'] = TITLE_CLEANUP_RE.subn('', name)[0]
        self.data['IPv4Network'] = ipaddress.IPv4Network(network)
        self.data['AWS Region'] = region

        self.data['Tags'] = []
        if tags is not None:
            if isinstance(tags, dict):
                for key, value in tags.items():
                    self.data['Tags'].append(Tag(Key=key, Value=value))
            elif isinstance(tags, list):
                self.data['Tags'] = tags

        self.data['AWS Credentials'] = {}
        self.data['AWS Credentials']['Access'] = aws_access_key
        self.data['AWS Credentials']['Secret'] = aws_secret_key
        self.data['Session'] = None
        self.data['Availability Zones'] = []
        self.data['Subnet Prefixlen'] = 0
        self.data['Template'] = Template()
        self.network = {}
        self.network['VPC'] = troposphere.ec2.VPC(
            title=self.data['Title'],
            CidrBlock=str(self.data['IPv4Network']),
            EnableDnsSupport=True,
            EnableDnsHostnames=True,
            InstanceTenancy='default',
            Tags=[Tag(Key='Name', Value=name)] + self.data['Tags'])
        self.data['Outputs'] = {}
        self.add_output(
            title=self.data['Title'] + TITLE_CLEANUP_RE.subn('', region)[0],
            description="VPC ID of {} in {}".format(name, region),
            value=Ref(self.network['VPC']),
            export=Sub('${AWS::StackName}-VPCID"'))
        if aws_profile is not None:
            self.data['Session'] = boto3.Session(profile_name=aws_profile,
                                                 region_name=region)
            creds = self.data['Session'].get_credentials()
            self.data['AWS Credentials']['Access'] = creds.access_key
            self.data['AWS Credentials']['Secret'] = creds.secret_key
        elif not None in (aws_access_key, aws_secret_key):
            self.data['Session'] = boto3.Session(
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                region_name=region)

    def add_output(self, title, description, value, export):
        """Safely add outputs to the template without duplicates"""
        if title not in self.data['Outputs']:
            self.data['Outputs'][title] = {}
            self.data['Outputs'][title]['Description'] = description
            self.data['Outputs'][title]['Value'] = value
            self.data['Outputs'][title]['Export'] = export
            self.data['Outputs'][title]['Output'] = Output(
                title=title,
                Description=description,
                Value=value,
                Export=Export(export))
            self.data['Template'].add_output(
                self.data['Outputs'][title]['Output'])
        else:
            raise Exception('{} is already an exported resource'.format(title))


    def get_availability_zones(self):
        """Populate availability zone data"""
        if not self.data['Availability Zones']:
            client = self.data['Session'].client('ec2')
            self.data['Availability Zones'] = \
                    [item['ZoneName']
                     for item in client.describe_availability_zones()[
                         'AvailabilityZones']]

    def calculate_subnet_prefixlen(self):
        """Divide up the network and calculate the prefix len for the subnets"""
        if not self.data['Availability Zones']:
            self.get_availability_zones()
        num_of_zones = len(self.data['Availability Zones']) * 2
        nearest = nearest_power_of_2(num_of_zones)
        subnets = [next(self.data['IPv4Network'].subnets(new_prefix=item))
                   for item in range(self.data['IPv4Network'].prefixlen + 1, 29)]
        subnet_hosts = [item.num_addresses for item in subnets]
        required_addresses = self.data['IPv4Network'].num_addresses / nearest
        try:
            subnet = subnets[subnet_hosts.index(required_addresses)]
        except ValueError:
            raise Exception("Provided network is too small to divide up to "
                            "use for all the availability zones in the region.")
        self.data['Subnet Prefixlen'] = subnet.prefixlen

    def create_internet_gateway(self):
        """Create an internet gateway if it does not exist already"""
        if 'InternetGateway' not in self.network:
            tag = Tag(Key='Name', Value='{} Internet Gateway'.format(
                self.data['Name']))
            self.network['InternetGateway'] = InternetGateway(
                title='{}InternetGateway'.format(self.data['Title']),
                template=self.data['Template'],
                Tags=[tag] + self.data['Tags'])
            tag = Tag(Key='Name', Value='{} VPC Gateway Attachment'.format(
                self.data['Name']))
            self.network['VPCGatewayAttachment'] = VPCGatewayAttachment(
                title='{}VPCGatewayAttachment'.format(self.data['Title']),
                template=self.data['Template'],
                VpcId=Ref(self.network['VPC']),
                InternetGatewayId=Ref(self.network['InternetGateway']))

    def create_public_subnet(self, zone, ip_network):
        """Create the public subnet and associated resources"""
        zone_title = self.data['Title'] + TITLE_CLEANUP_RE.subn('', zone)[0]
        tag = Tag(Key='Name',
                  Value='{} {} Public'.format(self.data['Name'], zone))
        subnet = Subnet(
            title=zone_title + 'Public',
            template=self.data['Template'],
            CidrBlock=ip_network,
            MapPublicIpOnLaunch=True,
            Tags=[tag] + self.data['Tags'],
            VpcId=Ref(self.network['VPC']))
        eip = EIP(
            title=zone_title + 'NatEIP',
            template=self.data['Template'],
            Domain='vpc',
            DependsOn='vpcgatewayattachment')
        tag = Tag(Key='Name',
                  Value='{} {} NAT Gateway'.format(
                      self.data['Name'], zone))
        natgateway = NatGateway(
            title=zone_title + 'NATGateway',
            template=self.data['Template'],
            AllocationId=GetAtt(zone_title + 'NatEIP', 'AllocationId'),
            SubnetId=Ref(self.network['VPC']),
            DependsOn=zone_title + 'NatEIP',
            Tags=[tag] + self.data['Tags'])
        tag = Tag(Key='Name',
                  Value='{} {} Public Route Table'.format(
                      self.data['Name'], zone))
        routetable = RouteTable(
            title=zone_title + 'PublicRouteTable',
            template=self.data['Template'],
            VpcId=Ref(self.network['VPC']),
            Tags=[tag] + self.data['Tags'])
        route = Route(
            title=zone_title + 'PublicDefaultRoute',
            template=self.data['Template'],
            DestinationCidrBlock='0.0.0.0/0',
            GatewayId=Ref(self.network['InternetGateway']),
            RouteTableId=Ref(routetable))
        subnetroutetableassociation = SubnetRouteTableAssociation(
            title=zone_title + 'PublicSubnetRouteTableAssociation',
            template=self.data['Template'],
            RouteTableId=Ref(routetable),
            SubnetId=Ref(subnet))

        self.network['Subnets'][zone]['Public'] = {}
        self.network['Subnets'][zone]['Public']['Subnet'] = subnet
        self.network['Subnets'][zone]['Public']['EIP'] = eip
        self.network['Subnets'][zone]['Public']['NatGateway'] = natgateway
        self.network['Subnets'][zone]['Public']['RouteTable'] = routetable
        self.network['Subnets'][zone]['Public']['DefaultRoute'] = route
        self.network['Subnets'][zone]['Public'][
            'SubnetRouteTableAssociation'] = subnetroutetableassociation

        self.add_output(
            title=self.data['Title'] + zone_title + 'PublicRouteTable',
            description="Public Route Table ID of {} in {}".format(
                self.data['Name'], zone),
            value=Ref(self.network['VPC']),
            export=Sub('${{AWS::StackName}}-{}-PublicRouteTable"'.format(
                zone)))

    def create_private_subnet(self, zone, ip_network):
        """Create private subnet and associated resources"""
        if not 'Public' in self.network['Subnets'][zone]:
            raise Exception(("Public subnet in {} does not exist to "
                             "create private subnet!").format(zone))
        elif not 'NatGateway' in self.network['Subnets'][zone]['Public']:
            raise Exception(("No NAT Gateway in public subnet {} to associate "
                             "default route with in private subnet!").format(
                                 zone))
        zone_title = self.data['Title'] + TITLE_CLEANUP_RE.subn('', zone)[0]
        tag = Tag(Key='Name',
                  Value='{} {} Private'.format(self.data['Name'], zone))
        subnet = Subnet(
            title=zone_title + 'Private',
            template=self.data['Template'],
            CidrBlock=ip_network,
            MapPublicIpOnLaunch=True,
            Tags=[tag] + self.data['Tags'],
            VpcId=Ref(self.network['VPC']))
        tag = Tag(Key='Name',
                  Value='{} {} Private Route Table'.format(
                      self.data['Name'], zone))
        routetable = RouteTable(
            title=zone_title + 'PrivateRouteTable',
            template=self.data['Template'],
            VpcId=Ref(self.network['VPC']),
            Tags=[tag] + self.data['Tags'])
        nat_gateway_id = Ref(self.network['Subnets'][zone]['Public'][
            'NatGateway'])
        route = Route(
            title=zone_title + 'PrivateDefaultRoute',
            template=self.data['Template'],
            DestinationCidrBlock='0.0.0.0/0',
            NatGatewayId=nat_gateway_id,
            RouteTableId=Ref(routetable))
        subnetroutetableassociation = SubnetRouteTableAssociation(
            title=zone_title + 'PrivateSubnetRouteTableAssociation',
            template=self.data['Template'],
            RouteTableId=Ref(routetable),
            SubnetId=Ref(subnet))
        self.network['Subnets'][zone]['Private'] = {}
        self.network['Subnets'][zone]['Private']['Subnet'] = subnet
        self.network['Subnets'][zone]['Private']['RouteTable'] = routetable
        self.network['Subnets'][zone]['Private']['DefaultRoute'] = route
        self.network['Subnets'][zone]['Private'][
            'SubnetRouteTableAssociation'] = subnetroutetableassociation

        self.add_output(
            title=self.data['Title'] + zone_title + 'PrivateRouteTable',
            description="Private Route Table ID of {} in {}".format(
                self.data['Name'], zone),
            value=Ref(self.network['VPC']),
            export=Sub('${{AWS::StackName}}-{}-PrivateRouteTable"'.format(
                zone)))

    def create_subnets(self):
        """Create all the public and private subnets"""
        self.get_availability_zones()
        if self.data['Subnet Prefixlen'] == 0:
            self.calculate_subnet_prefixlen()
        self.network['Subnets'] = {}
        subnets = self.data['IPv4Network'].subnets(new_prefix=self.data[
            'Subnet Prefixlen'])
        for zone in self.data['Availability Zones']:
            self.network['Subnets'][zone] = {}
            self.create_public_subnet(zone, str(next(subnets)))
            self.create_private_subnet(zone, str(next(subnets)))
