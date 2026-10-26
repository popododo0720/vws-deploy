module github.com/popododo0720/vws-network

go 1.26.0

require github.com/popododo0720/vws-proto v0.4.0

replace (
	github.com/popododo0720/vws-lib => ./third_party/vws-lib
	github.com/popododo0720/vws-proto v0.4.0 => ./third_party/vws-proto
)
