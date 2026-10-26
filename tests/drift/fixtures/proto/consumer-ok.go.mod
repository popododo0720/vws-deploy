module github.com/popododo0720/vws-compute

go 1.26.0

require (
	github.com/google/uuid v1.6.0
	github.com/popododo0720/vws-proto v0.5.0
	google.golang.org/grpc v1.79.3
)

require golang.org/x/net v0.48.0 // indirect

replace github.com/popododo0720/vws-lib => ./third_party/vws-lib
