"""Create the private S3 bucket that holds the call conversations (S3_TRANSCRIPT_BUCKET), if it does not exist.

Run from backend/:  python -m scripts.create_transcript_bucket

The bucket holds customers' original mobile numbers and what was said on the calls, so it is created with
all public access blocked and encryption on. An existing bucket is checked, and its settings are made safe
the same way. Nothing in the bucket is ever deleted by this script.
"""
from botocore.exceptions import ClientError

from config.settings import Settings
from copilot.utils.aws import aws_session


def main() -> None:
    settings = Settings()
    bucket, region = settings.s3_transcript_bucket, settings.aws_region
    if not bucket:
        raise SystemExit("S3_TRANSCRIPT_BUCKET is empty in config/.env")
    s3 = aws_session(settings).client("s3")
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"{bucket} already exists and is reachable.")
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise SystemExit(f"Cannot use {bucket}: {code}. {error.response['Error'].get('Message', '')}")
        args = {"Bucket": bucket}
        if region != "us-east-1":
            args["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**args)
        print(f"Created {bucket} in {region}.")
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
    )
    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    print("Public access blocked:", all(block.values()), "| encryption: AES256")


if __name__ == "__main__":
    main()
