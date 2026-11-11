package commands

import (
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/output"
)

func hostCmd(env *Env) *cobra.Command {
	var contentType string
	cmd := leaf("host <file>", "Host a reference file (image / audio / video) at a public URL that a vendor can fetch: "+
		"the image_urls / audio_urls an AIGC endpoint takes. 30 MB per file, 7-day TTL, free.",
		"treg host face.jpg", "treg host voice.mp3 --content-type audio/mpeg",
		"treg host face.jpg --json   # the full response: url, token, content_type, size, expires_at",
		`treg call reapi.video-gen.seedance-2-5 --data "{\"image_urls\":[\"$(treg host face.jpg)\"], ...}"`)
	cmd.Short = "Host a reference image / audio / video at a public URL for a vendor to fetch."
	cmd.Args = cobra.ExactArgs(1)
	cmd.Flags().StringVar(&contentType, "content-type", "", "override the type guessed from the extension")
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		path := config.ExpandUser(args[0])
		if !isRegularFile(path) {
			return output.Fail("treg host: file not found: %s", path)
		}
		name := filepath.Base(path)
		ctype := contentType
		if ctype == "" {
			ctype = guessMediaType(name)
		}
		if ctype == "" {
			return output.Fail("treg host: cannot guess the media type of %s; pass --content-type image/png (or audio/*, video/*)", name)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return fileError(err, path)
		}
		r, err := env.do(env.Client(), api.Request{Method: "POST", Path: "/media", Body: data,
			Header: [][2]string{{"content-type", ctype}}})
		if err != nil {
			return err
		}
		if r.Status >= 400 {
			_ = env.Out.Show(r)
			return &output.Exit{Code: 1}
		}
		b := responseObject(r)
		if env.JSON {
			outln(ojson.Pretty(b))
			return nil
		}
		outln(ojson.Text(b.Value("url")))
		errln("  " + ojson.Text(b.Value("content_type")) + ", " + ojson.Text(b.Value("size")) +
			" bytes, expires " + ojson.Text(b.Value("expires_at")))
		return nil
	}
	return cmd
}
