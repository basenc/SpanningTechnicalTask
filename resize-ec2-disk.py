#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Assessment task.
# CLI for resizing EC2 instance disk space.
# Task.
# Write CLI tool for resizing EC2 Primary EBS volume.
# Requirements:
# EC2 instance should has tag - Name.
# CLI should gets two parameters: EC2 Name, Adding disk space.
# NOTE implying EC2 Name is a "Name" instance tag
# Language: Python.
# Solution should be push to Public GitHub Repository.
# During: 3 days.

import argparse
import os
import botocore
import boto3
from time import sleep
from fabric import Connection, Config

AWS_API_ENDPOINT = "http://localhost:4566"
# AWS_API_ENDPOINT = "https://ec2.amazonaws.com"

REMOTE_USER = "ec2-user"
REMOTE_USER_PASS = "password"
REMOTE_SSH_PORT = 22


def stop_instance(i):
  i.stop()
  i.wait_until_stopped()


def start_instance(i):
  i.start()
  i.wait_until_running()


if __name__ == "__main__":
  parser = argparse.ArgumentParser()

  parser.add_argument("--size", "-s", type=int, required=True, help="Volume space to add")
  parser.add_argument("--name", "-n", type=str, required=True, help="Name of EC2 instance")

  args = parser.parse_args()

  # check AWS config
  if not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"}.issubset(os.environ):
    if not os.path.exists(f"{os.environ['HOME']}/.aws/credentials") or not os.path.exists(f"{os.environ['HOME']}/.aws/config"):
      raise SystemExit("AWS is not configured. Run 'aws config' or set correct environment variables.")

  # check AWS credentials validity
  try:
    sts = boto3.client("sts", endpoint_url=AWS_API_ENDPOINT)
    sts.get_caller_identity()
  except botocore.exceptions.ClientError as e:
    raise SystemExit(f"Bad AWS credentials: {e.response}")

  ec2_client = boto3.client("ec2", endpoint_url=AWS_API_ENDPOINT)
  ec2_resource = boto3.resource("ec2", endpoint_url=AWS_API_ENDPOINT)

  # find and filter instances
  instances = ec2_client.describe_instances(
      Filters=[
          {
              'Name': 'tag:Name',
              'Values': [args.name],
          },
      ]
  )

  if not instances["Reservations"]:
    raise SystemExit(f"Instance {args.name} not found")

  if len(instances["Reservations"]) > 1 or len(instances["Reservations"][0]["Instances"]) > 1:
    raise SystemExit(f"Multiple instances matched. This tool works only with single instance.")

  # get instance id and root volume id
  instance_id = instances["Reservations"][0]["Instances"][0]["InstanceId"]

  instance = ec2_resource.Instance(instance_id)
  instance_blockdev = instance.block_device_mappings

  if not [dev for dev in instance_blockdev if dev["DeviceName"] == instance.root_device_name]:
    raise SystemExit(f"This tool only supports EBS volumes (current type: {instance['RootDeviceType']})")

  volume_id = [dev["Ebs"]["VolumeId"] for dev in instance_blockdev if dev["DeviceName"] == instance.root_device_name][0]

  # stop instance
  stop_instance(instance)

  # snapshot root volume
  response = ec2_client.create_snapshot(
      Description='Autmatic snapshot',
      VolumeId=volume_id,
      TagSpecifications=[
          {
              'ResourceType': 'snapshot',
              'Tags': [
                  {
                      'Key': 'created_by',
                      'Value': 'resize-ec2-disk.py'
                  },
              ]
          },
      ]
  )
  snapshot = ec2_resource.Snapshot(response['SnapshotId'])

  # resize volume
  volume = ec2_resource.Volume(volume_id)
  ec2_client.modify_volume(
      VolumeId=volume_id,
      Size=volume.size + args.size
  )

  # restart instance
  start_instance(instance)

  # get instance public ip
  for i in instance.network_interfaces_attribute:
    if i["Association"]["PublicIp"] and i["Status"] == "in-use":
      public_ip = i["Association"]["PublicIp"]
      break
  else:
    raise SystemExit("Instance public IP not found")

  # ssh into instance and resize filesystem or fallback to previous snapshot if failed
  ssh_config = Config(overrides={'sudo': {'password': REMOTE_USER_PASS}})
  ssh = Connection(host=public_ip, user=REMOTE_USER, port=REMOTE_SSH_PORT, config=ssh_config)

  remote_os = ssh.run('uname -s')
  if remote_os != "Linux":
    raise SystemExit(f"Bad instance OS (got {remote_os})")

  remote_fstype = ssh.run(f"df -hT '{instance.root_device_name}' | tail -n +2 | awk '{{print $2}}'")

  try:
    if remote_fstype == "ext4":
      ssh.sudo(f"resize2fs {instance.root_device_name}")
    elif remote_fstype == "xfs":
      ssh.sudo(f"xfs_growfs -d /")
    else:
      raise SystemExit(f"Unsupported volume filesystem (got {remote_fstype})")
  except SystemExit as e:
    raise e
  except Exception as e:
    # create volume from snapshot
    response = ec2_client.create_volume(
        AvailabilityZone=volume.availability_zone,
        SnapshotId=snapshot.id,
        VolumeType=volume.volume_type,
    )
    backup_volume_id = response["VolumeId"]

    # stop instance
    stop_instance(instance)

    # detach old volume
    instance.detach_volume(
        Device=instance.root_device_name,
        Force=True,
        VolumeId=volume_id
    )

    # attach restored volume
    instance.attach_volume(
        Device=instance.root_device_name,
        VolumeId=backup_volume_id,
    )

    # restart instance
    start_instance(instance)

    # delete broken volume
    volume.delete()

    raise e
